#!/usr/bin/env python3
# Copyright (c) 2026 Durable State contributors | MIT License
"""
Drift detector: keep the shipped library and any vendored copy identical.

The problem it solves: when the same source lives in two places - the public
repository and inside some other system that vendored it - the copies drift.
Then you fix a bug in one place, keep testing against the other, and ship code
you no longer believe in. Both copies keep looking healthy the entire time.

This script compares hashes and tells you which side is lying.

    python tools/check_drift.py <source-dir> <vendored-dir> [name ...]
    python tools/check_drift.py SRC VENDORED --vendored-prefix _integrity_

The prefix option exists because of a gap found by using this tool for real: the
live system vendors these modules as _integrity_atomicio.py, so a detector that
only looks for identically-named files compares the product against an unrelated
shim and reports "drift" that is really a filename mismatch. A checker that
cannot be pointed at the case you actually have is not much of a checker.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

DEFAULT_NAMES = ("atomicio.py", "locks.py", "verify.py")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def find_sibling(root: Path) -> Path | None:
    for parent in root.parents:
        for candidate in (parent / "durable-state" / "durable_state", parent / "durable_state"):
            if candidate.is_dir():
                return candidate
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default=None, help="directory holding the authoritative files")
    ap.add_argument("vendored", nargs="?", default=None, help="directory holding the copies")
    ap.add_argument("names", nargs="*", default=None, help="file names to compare")
    ap.add_argument(
        "--vendored-prefix",
        default="",
        help="prefix applied to names on the vendored side, e.g. _integrity_",
    )
    args = ap.parse_args()

    here = Path(__file__).resolve().parent.parent

    source = Path(args.source).resolve() if args.source else (here if (here / "atomicio.py").exists() else find_sibling(here))
    if source is None:
        print("no source directory given and none found next to this repository")
        print("usage: python tools/check_drift.py <source-dir> <vendored-dir> [names...]")
        return 2

    if args.vendored:
        vendored = Path(args.vendored).resolve()
        names = args.names or list(DEFAULT_NAMES)
    else:
        print("drift check needs two directories")
        print("  source  : %s" % source)
        print("  usage   : python tools/check_drift.py %s <vendored-dir>" % source)
        return 2

    print("drift check: product <-> vendored copy")
    print("  product : %s" % source)
    print("  vendored: %s" % vendored)
    print("=" * 70)

    drift = 0
    for name in names:
        a, b = source / name, vendored / (args.vendored_prefix + name)
        if not a.exists():
            print("  MISSING IN PRODUCT : %s" % name)
            drift += 1
            continue
        if not b.exists():
            print("  MISSING IN VENDOR  : %s" % (args.vendored_prefix + name))
            drift += 1
            continue
        ha, hb = sha256(a), sha256(b)
        if ha == hb:
            print("  OK   %-16s %s" % (name, ha[:16]))
        else:
            print("  DRIFT %s" % name)
            print("         product : %s  %s" % (ha[:16], a))
            print("         vendored: %s  %s" % (hb[:16], b))
            drift += 1

    print("=" * 70)
    if drift:
        print("RESULT: %d file(s) diverged - the product and the copy are NOT the same code" % drift)
        return 1
    print("RESULT: identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
