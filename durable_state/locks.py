# Copyright (c) 2026 Durable State contributors | MIT License
"""
durable_state.locks - single-node file locks with crash recovery.

WHY THIS EXISTS
---------------
Two agents writing the same JSON memory file will corrupt it. A lock file is
the cheap answer, but a naive one has three failure modes:

1. DEADLOCK. The process dies holding the lock. Everyone waits forever.
2. THE EMPTY-WINDOW RACE. `os.open(O_CREAT|O_EXCL)` creates the file, but the
   owner has not written its identity into it yet. A second process that sees
   an empty lock must not conclude "stale, steal it" - it would steal a lock
   that is being legitimately acquired, right now. Fix: an empty lock younger
   than EMPTY_GRACE is assumed alive.
3. THE RELEASE RACE. Process A finishes, calls release(). But A's lock was
   already considered stale and removed by B, and B is now writing it. If
   release() blindly unlinks by name, A deletes B's lock. Fix: the owner writes
   a random token into the lock, and release() only unlinks when the token
   still matches. This module does that.

SCOPE - READ THIS BEFORE DEPLOYING
----------------------------------
SINGLE NODE, LOCAL FILESYSTEM ONLY. Verified on Windows (NTFS) only. Linux
(ext4) and macOS are expected to work, because the design uses `O_EXCL` and
unlink, but nothing here has been run and watched on either - treat them as
reasoned about, not demonstrated. Finding 9 is the reason that distinction
is not academic: its self-deadlock comes from Windows refusing to unlink a
file that any process holds open, which POSIX does not do, so the bug is
invisible there and cannot be found by reasoning from the Linux behaviour.

Specifically NOT safe on NFS: `O_EXCL` on NFS is not guaranteed atomic
(historically it is a plain open() on some servers), so two hosts can both
create the lock. If your agents run on several machines over a network
filesystem, use Redis, a database, or a real lock service. See FINDINGS.md.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = ["FileLock", "LockBusy", "acquire", "release", "is_locked", "lock_info"]

# An empty lock file is younger than this -> assume the owner is mid-write.
EMPTY_GRACE = 1.0
# A lock whose owner PID is gone on this host is considered abandoned.
STALE_PID_AGE = 300.0
# How long release() keeps retrying a delete that a reader is blocking.
# The blocker is another process running _read_owner(), which opens and closes
# the file in microseconds. 1.0 s is roughly a million times that window, so a
# give-up here means something is genuinely wrong, not that we were impatient.
UNLINK_GRACE = 1.0
UNLINK_POLL = 0.002


class LockBusy(RuntimeError):
    """The lock is held by someone else and could not be taken."""


def _lock_path(locks_dir: str | os.PathLike, name: str) -> Path:
    d = Path(locks_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / (name + ".lock")


def _pid_alive(pid: int) -> bool:
    """Is this PID running? Meaningful only on the local host."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def _read_owner(path: Path) -> dict | None:
    """
    Read the lock's owner record. Returns None if the lock is empty or corrupt.

    A corrupt lock is NOT automatically stale: it may be a partially written
    record. Age decides, via the caller's logic.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def is_locked(locks_dir: str | os.PathLike, name: str) -> bool:
    return _lock_path(locks_dir, name).exists()


def lock_info(locks_dir: str | os.PathLike, name: str) -> dict | None:
    """Introspection for a dashboard. Never raises - it says so, so it must.

    It is called from the LockBusy message, i.e. on a path where an exception
    would replace the real diagnosis with a confusing one. A lock can also
    disappear between the exists() check and stat(), which is not exotic when
    several processes are fighting over the same name.
    """
    p = _lock_path(locks_dir, name)
    try:
        age = round(time.time() - p.stat().st_mtime, 3)
    except OSError:
        return None
    info = _read_owner(p)
    if info is None:
        return {"state": "empty", "age": age}
    info = dict(info)
    info["state"] = "held"
    info["age"] = age
    info["pid_alive"] = _pid_alive(info.get("pid", -1)) if info.get("host") == socket.gethostname() else None
    return info


def _is_stale(path: Path, owner: dict | None) -> bool:
    """Decide whether an existing lock is abandoned. Conservative on purpose.

    Returns True if the lock is gone, because "gone" is not a reason to fail -
    the caller should just retry and win it. That case is real: a stress test
    found _is_stale() raising FileNotFoundError from path.stat() whenever the
    holder released in the microsecond between our os.open() and this call.
    A benign race turned into a crash in the caller's face.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True  # vanished underneath us: treat as stealable, caller retries
    if owner is None:
        # Empty or unparsable. A fresh one means the owner is still writing.
        return age > EMPTY_GRACE
    if owner.get("host") == socket.gethostname():
        if not _pid_alive(int(owner.get("pid", -1))):
            return True  # the owning process on this machine is gone
    # Other host, or a live local PID: only age can save us.
    return age > STALE_PID_AGE


def acquire(locks_dir: str | os.PathLike, name: str, timeout: float = 5.0, poll: float = 0.02) -> dict:
    """
    Take the lock and return our ownership record (including 'token').

    Blocking with a timeout. Raises LockBusy on timeout. Stale locks are taken
    over automatically; fresh empty ones are not.
    """
    path = _lock_path(locks_dir, name)
    token = uuid.uuid4().hex
    record = {
        "token": token,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "created": time.time(),
        "name": name,
    }
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                # Could not record who we are. Do not leave a nameless lock
                # behind: that is exactly the state that breaks others.
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                raise
            return record

        except FileExistsError:
            if _is_stale(path, _read_owner(path)):
                # Steal it. We do not unlink first: a plain unlink would open a
                # window where both parties believe they hold the lock. Instead
                # we move the stale file aside atomically, and only whoever wins
                # the rename gets to create the real lock.
                stale = path.with_suffix(".lock.stale.%d.%s" % (os.getpid(), uuid.uuid4().hex[:8]))
                try:
                    os.replace(str(path), str(stale))
                except OSError:
                    pass  # someone else beat us to it; retry the loop
                else:
                    try:
                        os.unlink(str(stale))
                    except OSError:
                        pass
                continue
            if time.time() >= deadline:
                raise LockBusy(
                    "lock %r is busy: %s" % (name, lock_info(locks_dir, name))
                )
            time.sleep(poll)


def _dbg(event: str, **kw) -> None:
    """Opt-in forensics. Silent unless DURABLE_STATE_DEBUG is set."""
    try:
        sys.stderr.write("DBG %s %s\n" % (event, json.dumps(kw, ensure_ascii=False, default=str)))
        sys.stderr.flush()
    except Exception:
        pass


def release(locks_dir: str | os.PathLike, name: str, record: dict) -> bool:
    """
    Release the lock ONLY if we still own it.

    Returns True if we released it, False if it was already gone or taken over
    by someone else. This is the fix for the release race: deleting by name
    alone lets a late-finishing process delete a lock that a different process
    now legitimately holds.
    """
    path = _lock_path(locks_dir, name)
    debug = bool(os.environ.get("DURABLE_STATE_DEBUG"))
    my_token = record.get("token")
    deadline = time.time() + UNLINK_GRACE

    while True:
        if not path.exists():
            if debug:
                _dbg("release.missing", name=name, mypid=os.getpid())
            return False
        current = _read_owner(path)
        if current is None:
            if debug:
                _dbg("release.unreadable", name=name, mypid=os.getpid())
            return False  # not ours any more
        if current.get("token") != my_token:
            if debug:
                _dbg(
                    "release.token_mismatch",
                    name=name,
                    mypid=os.getpid(),
                    my_token=my_token,
                    disk_token=current.get("token"),
                    disk_pid=current.get("pid"),
                )
            return False  # not ours any more
        try:
            os.unlink(str(path))
            return True
        except FileNotFoundError:
            return True  # someone removed it between our read and our unlink
        except OSError as exc:
            # THE WEDGE. On Windows a file that another process merely has open
            # for reading cannot be deleted until that handle closes, and
            # os.unlink() raises PermissionError instead of waiting. Giving up
            # here used to leak the lock file, and the next acquire() in this
            # same process then saw its OWN live pid in that file, called it
            # non-stale, and blocked for the full timeout. This was finding 9.
            #
            # The holder is mid-inspection and closes in microseconds, so the
            # right answer is to keep trying for a bounded moment - not to
            # abandon the lock and let the owner deadlock on itself.
            if time.time() >= deadline:
                if debug:
                    _dbg("release.unlink_gave_up", name=name, mypid=os.getpid(),
                         errno=exc.errno, winerr=getattr(exc, "winerror", None))
                return False
            if debug:
                _dbg("release.unlink_retry", name=name, mypid=os.getpid(),
                     errno=exc.errno, winerr=getattr(exc, "winerror", None))
            time.sleep(UNLINK_POLL)


class FileLock:
    """Context manager wrapper.

        with FileLock(lock_dir, "memory_graph"):
            write_something()

    Acquires, yields the ownership record, always releases (even on exception).
    """

    def __init__(self, locks_dir: str | os.PathLike, name: str, timeout: float = 5.0):
        self._dir = locks_dir
        self._name = name
        self._timeout = timeout
        self._record: dict | None = None

    def acquire(self) -> dict:
        if self._record is None:
            self._record = acquire(self._dir, self._name, self._timeout)
        return self._record

    def release(self) -> bool:
        if self._record is None:
            return False
        ok = release(self._dir, self._name, self._record)
        self._record = None
        return ok

    @contextmanager
    def __enter__(self) -> dict:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
