#!/usr/bin/env python3
# Copyright (c) 2026 Durable State contributors | MIT License
"""
Pre-publication audit. Run it before making a repository public, every time.

This exists because the previous version of this repository was one commit away
from being published with two defects that a visitor would have found before
reading a single line of the actual library:

  1. `tools/check_drift.py` - the leak detector - contained hardcoded private
     filesystem paths of the machine that wrote it. The auditor was the leak.
  2. `checksums.sha256` was committed, so it went stale on the very next commit
     and then claimed a file matched when it did not. An integrity tool that
     lies is worse than no integrity tool.

Both were found by running checks instead of assuming them. Keep running them.

    python tools/audit_release.py
Exit 0 = safe to publish. Exit 1 = do not publish yet.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".crash", ".venv", "build", "dist", ".tox"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".json", ".cfg", ".ini", ""}

# Things that must never appear in a public tree. Deliberately not just the
# token patterns: a pattern scanner finds credentials, and personal data is not
# a pattern. Both lists are needed.
SECRET_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), "github token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github fine-grained token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"), "private key"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|passwd|password)\s*=\s*[\"'][^\"']{8,}[\"']"), "inline secret"),
]
# IMPORTANT: these patterns were themselves buggy once. A raw string needs TWO
# backslashes to match ONE literal backslash, and the original used four, so it
# demanded a doubled separator and could never match a real Windows path. The
# single most important check in this file was dead code that always passed.
# Anything added here must be tested with tools/audit_selftest.py first.
# A Windows drive letter must NOT be preceded by a word character. Without that
# guard this pattern matches the tail of ordinary English text inside a string
# literal: an assertion message that ends with a word, then a colon, then a
# newline escape (backslash, n) looks exactly like drive D followed by a
# separator. That false positive blocked publication of two perfectly clean
# test files. Real paths appear as a quoted drive-letter string, a raw string,
# or at the start of a line, so a word character before the letter is a
# reliable signal that this is prose and not a path.
WINDOWS_ABS_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\(?:[A-Za-z0-9_.-]+\\)*[A-Za-z0-9_.-]+")
USER_HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/")
MOJIBAKE_MARKERS = ["\ufffd", "\u0420\u045a", "\u0420\u0455"]


def files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        out.append(p)
    return out


# A file that declares itself a fixture is skipped, and that fact is printed.
# This exists because the self-test necessarily contains fake tokens and fake
# private paths: without this, the audit flags its own test data forever. The
# exclusion is explicit and visible rather than a silent skip-list, and the
# self-test still proves those detectors fire.
FIXTURE_MARKER = "audit-fixture:"


def is_fixture(p: Path) -> bool:
    # The auditor is never a fixture, even though it contains the marker string
    # and the patterns themselves. Skipping it would disable the audit.
    if p.resolve() == Path(__file__).resolve():
        return False
    try:
        head = p.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return FIXTURE_MARKER in head


def main() -> int:
    problems: list[str] = []
    checked = 0
    fixtures: list[str] = []
    for p in files():
        rel = p.relative_to(ROOT).as_posix()
        if is_fixture(p):
            fixtures.append(rel)
            continue
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append("%s: has a UTF-8 BOM" % rel)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append("%s: not valid UTF-8 (%s)" % (rel, e.reason))
            continue
        checked += 1
        for pat, why in SECRET_PATTERNS:
            if pat.search(text):
                problems.append("%s: possible %s" % (rel, why))
        for m in WINDOWS_ABS_PATH.finditer(text):
            problems.append("%s: hardcoded absolute Windows path %r" % (rel, m.group(0)))
            break
        for m in USER_HOME_PATH.finditer(text):
            problems.append("%s: hardcoded user home path %r" % (rel, m.group(0)))
            break
        for marker in MOJIBAKE_MARKERS:
            if marker in text:
                problems.append("%s: mojibake marker U+%04X" % (rel, ord(marker[0])))
                break

    # A committed manifest always goes stale. Its presence is a defect, not a
    # feature, unless the maintainer deliberately regenerates it every commit.
    stale_manifest = ROOT / "checksums.sha256"
    if stale_manifest.exists():
        problems.append("checksums.sha256 is committed and will be stale on the next commit")

    print("pre-publication audit")
    print("=" * 70)
    print("  files scanned  : %d" % checked)
    if fixtures:
        print("  declared fixtures (skipped): %s" % ", ".join(fixtures))
    print("=" * 70)
    if problems:
        for p in problems:
            print("  BLOCKER %s" % p)
        print("=" * 70)
        print("RESULT: %d blocker(s). Do not publish." % len(problems))
        return 1
    print("RESULT: no personal paths, no secrets, no mojibake, no stale manifest.")
    print("        Safe to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
