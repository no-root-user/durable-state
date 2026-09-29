#!/usr/bin/env python3
# Copyright (c) 2026 Durable State contributors | MIT License
# audit-fixture: holds deliberately bad samples by design, so it is excluded
# from tools/audit_release.py. Fake tokens and fake home paths are the point.
"""
Self-test for the pre-publication audit.

Motivation: the audit shipped once with a regex that could never match anything.
It reported "clean" on a repository that contained hardcoded private paths,
which is worse than having no audit at all - it is false reassurance.

Rule this file enforces: every detector must be shown to FIRE on a known-bad
sample, and to stay quiet on a known-good one. A check that has never been seen
failing is not a check.

    python tools/audit_selftest.py
Exit 0 = the audit still detects what it claims to detect.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import audit_release  # noqa: E402

BS = chr(92)  # build backslashes numerically: no escape confusion in this file

CASES = [
    ("windows path", 'HOME = "D:' + BS + 'Users' + BS + 'someone' + BS + 'vika"', True),
    ("windows path short", 'P = "C:' + BS + 'temp' + BS + 'x"', True),
    ("posix home", 'X = "/home/someone/vika"', True),
    ("posix mac home", 'X = "/Users/someone/vika"', True),
    ("github token", 'T = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"', True),
    ("fine grained token", 'T = "github_pat_11ABCDEFG0aBcDeFgHiJkL_MnOpQrStUvWxYz0123456789"', True),
    ("private key", 'K = "-----BEGIN RSA PRIVATE KEY-----"', True),
    ("inline secret", 'password = "hunter2hunter2"', True),
    ("mojibake", 'X = "' + chr(0x0420) + chr(0x045A) + chr(0x0440) + chr(0x0438) + '"', True),
    ("replacement char", 'X = "' + chr(0xFFFD) + '"', True),
    # must NOT fire - the detector has to stay usable
    ("url with slash", 'URL = "https://example.com/path/to/file"', False),
    ("relative path", 'P = "durable_state/atomicio.py"', False),
    ("regex example in docs", 'R = r"[A-Za-z]:\\\\" + os.sep', False),
    ("prose about windows", 'TXT = "write to D: drive, then run"', False),
    # regressions from a real false positive: a word ending in a single letter,
    # then a colon, then an escape sequence, looks like drive D + separator.
    ("prose ending in d: then newline", 'assert x, "writer processes failed:\\n" + y', False),
    ("prose ending in k: then newline", 'msg = "acquire() raises on a vanished lock:\\n%s" % e', False),
    ("prose ending in h: then newline", 'm = "unexpected crash:\\n%s" % e', False),
    ("format spec then escape", 'L = "%s %04d\\n" % (tag, i)', False),
    # and the real thing must still fire. One escape level: these sample strings
    # are plain literals, so a doubled backslash here means two backslashes in
    # the sample, which no drive-letter pattern should match.
    ("quoted drive path", 'P = "D:\\Vi\\vika\\scripts\\locks.py"', True),
    ("raw drive path", 'P = r"C:\\Users\\me\\file.txt"', True),
    ("drive path at line start", 'D:\\Vi\\vika', True),
    ("utf8 cyrillic", 'X = "\u041f\u0440\u0438\u0432\u0435\u0442"', False),
]


def main() -> int:
    print("audit self-test: does the audit actually fire?")
    print("=" * 70)
    failed = []
    for name, sample, should_fire in CASES:
        hits = []
        for pat, why in audit_release.SECRET_PATTERNS:
            if pat.search(sample):
                hits.append(why)
        if audit_release.WINDOWS_ABS_PATH.search(sample):
            hits.append("windows abs path")
        if audit_release.USER_HOME_PATH.search(sample):
            hits.append("user home path")
        for marker in audit_release.MOJIBAKE_MARKERS:
            if marker in sample:
                hits.append("mojibake U+%04X" % ord(marker[0]))
                break
        fired = bool(hits)
        ok = fired == should_fire
        print("  %-5s %-22s expected=%-5s got=%-5s %s"
              % ("OK" if ok else "FAIL", name, should_fire, fired, ",".join(hits)))
        if not ok:
            failed.append(name)
    print("=" * 70)
    if failed:
        print("RESULT: %d detector(s) misbehave: %s" % (len(failed), failed))
        return 1
    print("RESULT: every detector fires on its bad sample and stays quiet on the good ones")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
