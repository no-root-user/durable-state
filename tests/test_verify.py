# Copyright (c) 2026 Vika Integrity contributors | MIT License
"""Tests for verify. The point: it must FAIL on damage, not just pass on health."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vika_integrity.atomicio import atomic_write_text  # noqa: E402
from vika_integrity.verify import sha256_of, verify_dir  # noqa: E402


def build(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    atomic_write_text(tmp_path / "a.txt", "hello\n")
    atomic_write_text(tmp_path / "data" / "b.json", '{"x": 1}\n')
    atomic_write_text(
        tmp_path / "checksums.sha256",
        "# manifest\n"
        + "%s  a.txt\n" % sha256_of(tmp_path / "a.txt")
        + "%s  data/b.json\n" % sha256_of(tmp_path / "data" / "b.json"),
    )
    return tmp_path


def test_clean_package_passes(tmp_path: Path) -> None:
    r = verify_dir(build(tmp_path))
    assert r["ok"], r
    assert r["checked"] == 2
    assert not r["missing"] and not r["hash_mismatch"]


def test_modified_file_is_caught(tmp_path: Path) -> None:
    build(tmp_path)
    atomic_write_text(tmp_path / "a.txt", "tampered\n")
    r = verify_dir(tmp_path)
    assert not r["ok"]
    assert "a.txt" in r["hash_mismatch"]


def test_missing_file_is_caught(tmp_path: Path) -> None:
    build(tmp_path)
    (tmp_path / "a.txt").unlink()
    r = verify_dir(tmp_path)
    assert not r["ok"]
    assert "a.txt" in r["missing"]


def test_corrupt_utf8_is_caught(tmp_path: Path) -> None:
    """A file that is not decodable as UTF-8 is a real finding."""
    build(tmp_path)
    # write raw bytes that are NOT valid UTF-8, keeping the manifest in sync
    (tmp_path / "a.txt").write_bytes(b"\xff\xfe\x00bad")
    # update the manifest so ONLY the encoding is wrong, not the hash
    manifest = (tmp_path / "checksums.sha256").read_text(encoding="utf-8").replace(
        "%s  a.txt" % sha256_of(tmp_path / "a.txt"),
        "%s  a.txt" % sha256_of(tmp_path / "a.txt"),
    )
    # recompute properly: put the true hash of the corrupt file
    lines = []
    for line in manifest.splitlines():
        if line.endswith("a.txt"):
            line = "%s  a.txt" % sha256_of(tmp_path / "a.txt")
        lines.append(line)
    atomic_write_text(tmp_path / "checksums.sha256", "\n".join(lines) + "\n")

    r = verify_dir(tmp_path)
    assert not r["ok"]
    assert any("a.txt" in x for x in r["not_utf8"])


def test_bom_is_flagged(tmp_path: Path) -> None:
    build(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"\xef\xbb\xbfhello\n")
    lines = []
    for line in (tmp_path / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        if line.endswith("a.txt"):
            line = "%s  a.txt" % sha256_of(tmp_path / "a.txt")
        lines.append(line)
    atomic_write_text(tmp_path / "checksums.sha256", "\n".join(lines) + "\n")
    r = verify_dir(tmp_path)
    assert "a.txt" in r["has_bom"]
    assert not r["ok"]


def test_binary_is_not_judged_by_encoding(tmp_path: Path) -> None:
    """A .mp4 is checked by hash only. Judging a video as text is nonsense."""
    build(tmp_path)
    blob = bytes(range(256)) * 10
    (tmp_path / "clip.mp4").write_bytes(blob)
    atomic_write_text(
        tmp_path / "checksums.sha256",
        (tmp_path / "checksums.sha256").read_text(encoding="utf-8")
        + "%s  clip.mp4\n" % sha256_of(tmp_path / "clip.mp4"),
    )
    r = verify_dir(tmp_path)
    assert r["ok"], r
    assert not r["not_utf8"]


def test_missing_manifest_reports_error(tmp_path: Path) -> None:
    r = verify_dir(tmp_path)
    assert not r["ok"]
    assert "error" in r


def test_unlisted_files_are_informational(tmp_path: Path) -> None:
    build(tmp_path)
    atomic_write_text(tmp_path / "extra.txt", "surprise\n")
    r = verify_dir(tmp_path)
    assert r["ok"]  # does not break the verdict
    assert "extra.txt" in r["unlisted"]  # but is reported


def test_self_healing_pattern(tmp_path: Path) -> None:
    """
    Real usage: verify on startup, refuse to continue if the brain is damaged.
    A normal system keeps going and hallucinates over a corrupted memory file.
    """
    build(tmp_path)
    assert verify_dir(tmp_path)["ok"]
    (tmp_path / "a.txt").write_text("silently truncated")
    decision = "run" if verify_dir(tmp_path)["ok"] else "STOP_AND_REPAIR"
    assert decision == "STOP_AND_REPAIR"
