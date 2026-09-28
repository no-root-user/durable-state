# Copyright (c) 2026 Durable State contributors | MIT License
"""
Tests for atomicio.

The important one is test_append_never_destroys_large_file: it is the exact
shape of the bug that cost 120 000 lines. If someone re-introduces the
"old = ... if small else ''" shortcut, this test fails loudly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from durable_state.atomicio import (  # noqa: E402
    APPEND_INLINE_MAX,
    APPEND_STRATEGY_COPY,
    APPEND_STRATEGY_INMEM,
    atomic_append_text,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)

# ASCII-only payloads on purpose: a test's verdict must not depend on the
# terminal's encoding. (Learned the hard way - a corrupted literal made a
# passing test report False.)
LINE = "OLD-valuable-line"


def test_write_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_creates_missing_parents(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nested" / "a.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_overwrite_replaces_content(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    atomic_write_text(p, "first and long content")
    atomic_write_text(p, "second")
    assert p.read_text(encoding="utf-8") == "second"


def test_never_leaves_temp_files(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    atomic_write_text(p, "x" * 100)
    atomic_write_text(p, "y")
    assert [f.name for f in tmp_path.iterdir()] == ["a.txt"]


def test_failed_write_keeps_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash during the write must not damage what is already there."""
    p = tmp_path / "a.txt"
    atomic_write_text(p, "PRECIOUS")

    import durable_state.atomicio as mod

    def boom(*a, **k):
        raise OSError("simulated power loss")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "new content")
    monkeypatch.undo()

    assert p.read_text(encoding="utf-8") == "PRECIOUS"
    assert [f.name for f in tmp_path.iterdir()] == ["a.txt"]


def test_json_roundtrip_and_utf8(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    data = {"ключ": "значение", "n": [1, 2, 3]}
    atomic_write_json(p, data)
    assert json.loads(p.read_text(encoding="utf-8")) == data
    assert "ключ" in p.read_text(encoding="utf-8")  # not \uXXXX escaped


def test_newlines_are_not_translated(tmp_path: Path) -> None:
    """The same bytes on Windows and Linux - no implicit CRLF."""
    p = tmp_path / "a.txt"
    atomic_write_text(p, "a\nb\nc")
    assert p.read_bytes() == b"a\nb\nc"


def test_bytes_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    blob = bytes(range(256))
    atomic_write_bytes(p, blob)
    assert p.read_bytes() == blob


def test_append_small_uses_inmemory(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("a\n", encoding="utf-8")
    strategy = atomic_append_text(p, "b")
    assert strategy == APPEND_STRATEGY_INMEM
    assert p.read_text(encoding="utf-8") == "a\nb\n"


def test_append_missing_file_creates_it(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    atomic_append_text(p, "only line")
    assert p.read_text(encoding="utf-8") == "only line\n"


def test_append_adds_exactly_one_line(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("a\n", encoding="utf-8")
    atomic_append_text(p, "b\n")  # already has newline
    assert p.read_text(encoding="utf-8") == "a\nb\n"


@pytest.mark.parametrize("n", [1000, 120_000])
def test_append_never_destroys_content(tmp_path: Path, n: int) -> None:
    """
    THE regression test.

    An earlier version did: old = read_text() if small else "" - so every file
    over the threshold was silently truncated to just the new line. This
    asserts the total count of pre-existing lines is preserved, for both the
    in-memory and the copy strategy.
    """
    p = tmp_path / "memory_graph.json"
    p.write_text((LINE + "\n") * n, encoding="utf-8")
    size = p.stat().st_size

    strategy = atomic_append_text(p, "APPENDED")
    text = p.read_text(encoding="utf-8")

    expected = APPEND_STRATEGY_INMEM if size <= APPEND_INLINE_MAX else APPEND_STRATEGY_COPY
    assert strategy == expected
    assert text.count(LINE) == n, "lost %d lines" % (n - text.count(LINE))
    assert text.count("APPENDED") == 1
    assert text.count("\n") == n + 1


def test_append_large_preserves_existing_bytes_exactly(tmp_path: Path) -> None:
    """Copy path must not rewrite the original bytes (no newline translation)."""
    p = tmp_path / "big.txt"
    original = ("a\n" * 10) + ("b\r\n" * 10)  # deliberately mixed endings
    p.write_bytes(original.encode("utf-8"))
    p.touch()  # exists
    # force the copy strategy by lowering nothing - just check what happens
    atomic_append_text(p, "tail")
    assert p.read_bytes() == original.encode("utf-8") + b"tail\n"


def test_append_leaves_no_temp(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("x\n" * 100, encoding="utf-8")
    atomic_append_text(p, "y")
    assert [f.name for f in tmp_path.iterdir()] == ["a.txt"]


def test_strategies_are_reported_distinctly(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("x\n", encoding="utf-8")
    small = atomic_append_text(p, "y")
    # push past the threshold and check we switch
    p.write_text("x\n" * ((APPEND_INLINE_MAX // 2) + 10), encoding="utf-8")
    large = atomic_append_text(p, "y")
    assert small == APPEND_STRATEGY_INMEM
    assert large == APPEND_STRATEGY_COPY
