# Copyright (c) 2026 Vika Integrity contributors | MIT License
"""
vika_integrity.atomicio - crash-safe file writes.

WHY THIS EXISTS
---------------
`path.write_text()` is not atomic. It truncates the file, then writes, then
closes. A crash (power loss, OOM kill, Ctrl-C, full disk) between truncate and
close leaves you with an empty or half-written file. For an AI memory store
that is silent, permanent data loss.

The fix is the standard POSIX pattern: write to a temporary file in the SAME
directory, fsync it, then `os.replace()` the original. `os.replace()` is atomic
on POSIX (rename(2)) and on Windows (MoveFileEx with MOVEFILE_REPLACE_EXISTING),
so a reader sees either the whole old file or the whole new one - never half.

Same directory is not a detail: rename() is only atomic within one filesystem,
so the temp file must not live in %TEMP%.

This module was extracted from a real project where a 4.8 MB / 120 000-line
memory file was destroyed by exactly this bug. See FINDINGS.md bug #1.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_text",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_append_text",
    "atomic_replace",
    "APPEND_INLINE_MAX",
    "APPEND_STRATEGY_COPY",
    "APPEND_STRATEGY_INMEM",
]

# Files at or below this size are rewritten in memory: one write, no copy.
# Above it we stream: the file gets copied to a temp file, one line is
# appended to the copy, and the copy replaces the original. That costs a full
# copy (O(size)) but bounds peak memory, which is the reason the threshold
# exists at all.
APPEND_INLINE_MAX = 8 * 1024 * 1024

APPEND_STRATEGY_INMEM = "in-memory"
APPEND_STRATEGY_COPY = "copy-then-append"


def _same_dir_temp(target: Path, suffix: str = "") -> Path:
    """Temp path in the target's own directory - required for atomic rename."""
    fd, name = tempfile.mkstemp(
        prefix="." + target.name + ".", suffix=suffix + ".tmp", dir=str(target.parent)
    )
    os.close(fd)
    return Path(name)


def _fsync_dir(directory: Path) -> None:
    """
    fsync the directory so the rename itself is durable.

    Without this, the rename can be lost on power failure even though the file
    data was fsynced: the data block is on disk, the directory entry is not.
    Best effort: Windows and some filesystems do not allow opening a directory.
    """
    try:
        fd = os.open(str(directory), getattr(os, "O_DIRECTORY", 0))
    except (OSError, AttributeError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_replace(temp: Path, target: Path) -> None:
    """Durably move temp over target. This is the atomic step."""
    os.replace(str(temp), str(target))
    _fsync_dir(target.parent)


def atomic_write_bytes(target: str | os.PathLike, data: bytes) -> None:
    """Write bytes so the target is never observed partially written."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _same_dir_temp(target, ".bytes")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, target)
    except BaseException:
        _silent_unlink(tmp)
        raise


def atomic_write_text(
    target: str | os.PathLike, text: str, encoding: str = "utf-8", newline: str = ""
) -> None:
    """
    Write text atomically.

    newline="" is the default ON PURPOSE: it stops Python from translating
    "\n" into "\r\n" on Windows, so a file written on Windows and the same file
    written on Linux are byte-identical. If your content already contains the
    line endings you want, they are preserved as-is.
    """
    atomic_write_bytes(target, text.encode(encoding))


def atomic_write_json(target: str | os.PathLike, obj: Any, encoding: str = "utf-8") -> None:
    """Serialise to JSON, then write it atomically (indent=2, real UTF-8)."""
    import json

    atomic_write_text(target, json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding)


def _append_strategy(size: int) -> str:
    return APPEND_STRATEGY_INMEM if size <= APPEND_INLINE_MAX else APPEND_STRATEGY_COPY


def atomic_append_text(
    target: str | os.PathLike, line: str, encoding: str = "utf-8", newline: str = ""
) -> str:
    """
    Append one line atomically. Returns the strategy actually used.

    BUG THIS FIXES: an earlier version did
        old = read_text() if size <= LIMIT else ""   # <- data destroyed
        atomic_write_text(target, old + line)
    so any file over the limit was REWRITTEN EMPTY, then got the new line.
    A 4.8 MB / 120 000-line file became 25 bytes. Silent, total data loss.

    Now the size only chooses HOW to append, never WHETHER to keep the content:
      - in-memory: read, append, atomic rewrite.
      - copy-then-append: copy the original to a temp file in the same
        directory, append to the copy, atomically replace. Memory stays flat.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = line if line.endswith("\n") else line + "\n"

    if not target.exists():
        atomic_write_bytes(target, line.encode(encoding))
        return APPEND_STRATEGY_INMEM

    size = target.stat().st_size
    strategy = _append_strategy(size)

    if strategy == APPEND_STRATEGY_INMEM:
        # Path.read_text() only grew `newline` in 3.13; on 3.12 it raises
        # TypeError, so open() explicitly. Found by the test suite, not by me.
        with open(target, "r", encoding=encoding, newline="") as f:
            old = f.read()
        atomic_write_text(target, old + line, encoding, newline)
        return strategy

    tmp = _same_dir_temp(target, ".append")
    try:
        # binary copy first: preserves existing bytes exactly, no newline
        # translation, no decoding cost
        shutil.copyfile(str(target), str(tmp))
        with open(tmp, "ab") as f:
            f.write(line.encode(encoding))
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, target)
    except BaseException:
        _silent_unlink(tmp)
        raise
    return strategy


def _silent_unlink(p: Path) -> None:
    """Best-effort temp cleanup. Never masks the original error."""
    try:
        os.unlink(str(p))
    except OSError:
        pass
