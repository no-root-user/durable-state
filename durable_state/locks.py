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
SINGLE NODE, LOCAL FILESYSTEM ONLY. Verified on Windows (NTFS) and Linux
(ext4). Specifically NOT safe on NFS: `O_EXCL` on NFS is not guaranteed
atomic (historically it is a plain open() on some servers), so two hosts can
both create the lock. If your agents run on several machines over a network
filesystem, use Redis, a database, or a real lock service. See FINDINGS.md.
"""
from __future__ import annotations

import json
import os
import socket
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
    """Introspection for a dashboard. Never raises."""
    p = _lock_path(locks_dir, name)
    if not p.exists():
        return None
    info = _read_owner(p)
    if info is None:
        return {"state": "empty", "age": round(time.time() - p.stat().st_mtime, 3)}
    info = dict(info)
    info["state"] = "held"
    info["age"] = round(time.time() - p.stat().st_mtime, 3)
    info["pid_alive"] = _pid_alive(info.get("pid", -1)) if info.get("host") == socket.gethostname() else None
    return info


def _is_stale(path: Path, owner: dict | None) -> bool:
    """Decide whether an existing lock is abandoned. Conservative on purpose."""
    age = time.time() - path.stat().st_mtime
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


def release(locks_dir: str | os.PathLike, name: str, record: dict) -> bool:
    """
    Release the lock ONLY if we still own it.

    Returns True if we released it, False if it was already gone or taken over
    by someone else. This is the fix for the release race: deleting by name
    alone lets a late-finishing process delete a lock that a different process
    now legitimately holds.
    """
    path = _lock_path(locks_dir, name)
    if not path.exists():
        return False
    current = _read_owner(path)
    if current is None or current.get("token") != record.get("token"):
        return False  # not ours any more
    try:
        os.unlink(str(path))
        return True
    except OSError:
        return False


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
