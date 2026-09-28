# Copyright (c) 2026 Vika Integrity contributors | MIT License
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
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vika_integrity.locks import (  # noqa: E402
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
