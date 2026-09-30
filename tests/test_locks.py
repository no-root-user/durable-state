# Copyright (c) 2026 Durable State contributors | MIT License
"""
Tests for locks.

Covers the three failure modes named in the module docstring: deadlock,
the empty-window race, and the release race. The last one has a dedicated
test because it was a real bug class: release() used to unlink by name, so a
slow process could delete a lock that another process had already taken over.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from durable_state.locks import (  # noqa: E402
    EMPTY_GRACE,
    FileLock,
    LockBusy,
    _lock_path,
    acquire,
    is_locked,
    lock_info,
    release,
)


def test_acquire_and_release(tmp_path: Path) -> None:
    rec = acquire(tmp_path, "memory")
    assert is_locked(tmp_path, "memory")
    assert rec["pid"] == os.getpid()
    assert release(tmp_path, "memory", rec) is True
    assert not is_locked(tmp_path, "memory")


def test_second_acquire_times_out(tmp_path: Path) -> None:
    rec = acquire(tmp_path, "memory")
    with pytest.raises(LockBusy):
        acquire(tmp_path, "memory", timeout=0.2)
    release(tmp_path, "memory", rec)


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    try:
        with FileLock(tmp_path, "memory"):
            assert is_locked(tmp_path, "memory")
            raise ValueError("boom")
    except ValueError:
        pass
    assert not is_locked(tmp_path, "memory")


def test_dead_owner_pid_is_stale(tmp_path: Path) -> None:
    """
    A lock left by a PID that no longer exists on THIS host must be stealable.

    Note the host must really be ours: the module refuses to trust a PID check
    across machines (we cannot see another host's process table), so an unknown
    host falls back to age-based staleness. That is intentional, and the test
    pins the behaviour rather than faking a hostname.
    """
    import socket

    p = _lock_path(tmp_path, "memory")
    p.write_text(
        json.dumps(
            {
                "token": "x",
                "pid": 999_999,  # not a running process
                "host": socket.gethostname(),
                "created": time.time(),
            }
        ),
        encoding="utf-8",
    )
    rec = acquire(tmp_path, "memory", timeout=0.5)
    assert rec is not None
    release(tmp_path, "memory", rec)


def test_unknown_host_falls_back_to_age(tmp_path: Path) -> None:
    """A lock from a host we cannot verify is NOT reclaimed just for a dead PID."""
    p = _lock_path(tmp_path, "memory")
    p.write_text(
        json.dumps(
            {"token": "x", "pid": 999_999, "host": "some-other-machine", "created": time.time()}
        ),
        encoding="utf-8",
    )
    with pytest.raises(LockBusy):
        acquire(tmp_path, "memory", timeout=0.2)


def test_fresh_empty_lock_is_respected(tmp_path: Path) -> None:
    """
    THE empty-window regression.

    Owner created the lock but has not written its identity yet. A second
    process must wait, not steal - otherwise we break a lock that is being
    legitimately acquired right now.
    """
    p = _lock_path(tmp_path, "memory")
    p.write_text("", encoding="utf-8")
    with pytest.raises(LockBusy):
        acquire(tmp_path, "memory", timeout=0.2)


def test_old_empty_lock_is_stale(tmp_path: Path) -> None:
    p = _lock_path(tmp_path, "memory")
    p.write_text("", encoding="utf-8")
    old = time.time() - EMPTY_GRACE - 1
    os.utime(p, (old, old))
    rec = acquire(tmp_path, "memory", timeout=0.5)
    release(tmp_path, "memory", rec)


def test_release_by_non_owner_is_refused(tmp_path: Path) -> None:
    """
    THE release-race regression.

    If we did not take this lock, we must not be able to release it. Blind
    unlink-by-name would let a stale process kill a live lock.
    """
    rec = acquire(tmp_path, "memory")
    impostor = dict(rec, token="not-my-token")
    assert release(tmp_path, "memory", impostor) is False
    assert is_locked(tmp_path, "memory")
    release(tmp_path, "memory", rec)


def test_release_twice_is_safe(tmp_path: Path) -> None:
    rec = acquire(tmp_path, "memory")
    assert release(tmp_path, "memory", rec) is True
    assert release(tmp_path, "memory", rec) is False


def test_two_locks_do_not_collide(tmp_path: Path) -> None:
    a = acquire(tmp_path, "alpha")
    b = acquire(tmp_path, "beta")
    assert a["token"] != b["token"]
    release(tmp_path, "alpha", a)
    release(tmp_path, "beta", b)


def test_lock_info_shape(tmp_path: Path) -> None:
    assert lock_info(tmp_path, "nothing") is None
    rec = acquire(tmp_path, "memory")
    info = lock_info(tmp_path, "memory")
    assert info and info["state"] == "held"
    assert info["token"] == rec["token"]
    assert isinstance(info["age"], (int, float))
    release(tmp_path, "memory", rec)


def test_nested_reacquire_is_idempotent(tmp_path: Path) -> None:
    fl = FileLock(tmp_path, "memory")
    r1 = fl.acquire()
    r2 = fl.acquire()
    assert r1 is r2
    fl.release()
    assert not is_locked(tmp_path, "memory")


# ---------------------------------------------------------------------------
# Finding 9: the self-deadlock. Reproduced here deterministically, because the
# original report could only catch it about a third of the time.
#
# The mechanism: on Windows, os.unlink() cannot delete a file that any process
# holds open for reading, and raises PermissionError rather than waiting. The
# contender in acquire() holds exactly such a handle while it inspects the lock
# via _read_owner(). release() used to treat that as final, return False, and
# leave the lock file behind - with THIS process's own live pid written in it.
#
# The next acquire() in the same process then read that pid, judged the lock
# alive and therefore not stale, and blocked for the entire timeout waiting for
# a lock nobody else was holding. That is the "self-deadlock" in the stress
# report: the blocking PID and the blamed PID were the same number.
# ---------------------------------------------------------------------------


def test_release_completes_while_a_reader_holds_the_file_open(tmp_path: Path) -> None:
    """
    The regression itself: release() must ride out a reader that is mid-
    inspection, instead of abandoning the lock on its first PermissionError.

    A real contender holds the handle for microseconds. The old code treated
    that as permanent and returned False, leaking the lock.
    """
    rec = acquire(tmp_path, "memory")
    path = _lock_path(tmp_path, "memory")

    if not sys.platform == "win32":
        pytest.skip("the blocking-delete behaviour is specific to Windows")

    # While the handle is open, a plain unlink is impossible. Prove the
    # precondition, so a pass cannot come from the platform being kind.
    blocker = open(path, "r", encoding="utf-8")
    blocker.read()
    with pytest.raises(OSError):
        os.unlink(str(path))

    def reader():
        time.sleep(0.05)  # a real contender finishes about this fast
        blocker.close()

    t = threading.Thread(target=reader)
    t.start()
    try:
        assert release(tmp_path, "memory", rec) is True, (
            "release gave up while a reader was mid-inspection - that is the "
            "lock leak that caused finding 9"
        )
    finally:
        t.join()
    assert not is_locked(tmp_path, "memory")


def test_release_gives_up_eventually_on_a_permanent_blocker(tmp_path: Path) -> None:
    """
    The other half of the contract: retrying is bounded. A handle that never
    closes must not turn release() into an infinite hang, and a lock we cannot
    delete must be reported as not released rather than silently claimed.
    """
    rec = acquire(tmp_path, "memory")
    path = _lock_path(tmp_path, "memory")

    blocker = open(path, "r", encoding="utf-8")
    blocker.read()
    try:
        started = time.time()
        ok = release(tmp_path, "memory", rec)
        waited = time.time() - started
        assert ok is False
        assert waited < 10.0, "release() blocked for %0.1fs, it must be bounded" % waited
        assert is_locked(tmp_path, "memory"), "lock vanished although we could not delete it"
    finally:
        blocker.close()

    # Once the blocker is gone the lock is reclaimable: our own pid is in it,
    # but the file is old enough to count as abandoned.
    from durable_state.locks import _is_stale, _read_owner
    old = time.time() - 400
    os.utime(str(path), (old, old))
    assert _is_stale(path, _read_owner(path)) is True


def test_no_lock_leak_means_no_self_deadlock(tmp_path: Path) -> None:
    """
    The consequence, stated as a test: if release() never leaks, a process can
    never find its own live pid in a lock it should be able to take.

    This is the shape of the original failure, with the flaky timing removed:
    the old code failed this roughly a third of the time.
    """
    def reader(path: Path) -> None:
        time.sleep(0.01)
        try:
            with open(path, "r", encoding="utf-8") as f:
                f.read()
        except OSError:
            pass

    path = _lock_path(tmp_path, "memory")
    for _ in range(50):
        rec = acquire(tmp_path, "memory", timeout=5)
        t = threading.Thread(target=reader, args=(path,))
        t.start()
        t.join()  # reader is done, so this release is uncontended
        release(tmp_path, "memory", rec)
        assert not path.exists(), "lock leaked - the next acquire would deadlock on itself"

    # And the follow-up acquire is instant rather than waiting out a timeout.
    started = time.time()
    rec = acquire(tmp_path, "memory", timeout=5)
    assert time.time() - started < 1.0, "acquire had to wait: a lock of ours leaked earlier"
    release(tmp_path, "memory", rec)


def test_release_still_refuses_a_lock_we_do_not_own(tmp_path: Path) -> None:
    """
    The retry loop must not weaken ownership. Retrying the unlink is allowed;
    deleting somebody else's lock is not.
    """
    rec = acquire(tmp_path, "memory")
    impostor = dict(rec, token="not-our-token")
    assert release(tmp_path, "memory", impostor) is False
    assert is_locked(tmp_path, "memory"), "released a lock we never owned"
    assert release(tmp_path, "memory", rec) is True


def test_version_matches_pyproject(tmp_path: Path) -> None:
    """
    The version lived in two files and they drifted.

    __init__.py said 0.1.0 while pyproject.toml said 0.1.0, and the tags said
    0.1.2 - three numbers, no single source of truth, and nothing to notice when
    they disagreed. The same failure as finding 10 in prose, except this one was
    reachable by arithmetic, so it is worth a test instead of a promise.

    Keeping two copies is a choice; letting them silently disagree is not.
    """
    import re

    from durable_state import __version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no version field"
    assert __version__ == match.group(1), (
        "durable_state.__version__ is %r but pyproject.toml says %r. One of "
        "them was not bumped." % (__version__, match.group(1))
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), (
        "version %r is not semver" % __version__
    )
