#!/usr/bin/env python3
# Copyright (c) 2026 Vika Integrity contributors | MIT License
"""
Local CI runner - the same checks as .github/workflows/ci.yml, on whatever
machine you happen to be on.

Why it exists: the deploy token used for this repository lacks GitHub's
`workflow` scope, so the workflow cannot be pushed. Rather than rename the file
to dodge the restriction, keep the definition visible and runnable.

    python tools/local_ci.py            # tests + crash harness
    python tools/local_ci.py --quick    # tests only

What it CANNOT do: verify Linux or macOS. It reports the platform it ran on, and
refuses to claim anything about the others. A green run here means "green on
this machine", not "green everywhere".
"""
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(label: str, argv: list[str]) -> bool:
    print("\n" + "=" * 68)
    print("== %s" % label)
    print("=" * 68)
    proc = subprocess.run(argv, cwd=str(ROOT))
    ok = proc.returncode == 0
    print("--> %s: %s" % (label, "PASS" if ok else "FAIL"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the crash harness")
    ap.add_argument("--rounds", type=int, default=25)
    args = ap.parse_args()

    print("platform : %s %s (%s)" % (platform.system(), platform.release(), platform.machine()))
    print("python   : %s" % sys.version.split()[0])
    if platform.system() not in ("Windows", "Linux", "Darwin"):
        print("WARNING  : unrecognised platform, results will not generalise")

    results = [run("unit tests", [sys.executable, "-m", "pytest", "tests", "-v"])]
    if not args.quick:
        results.append(
            run("crash harness", [sys.executable, "tests/crash_harness.py", "--rounds", str(args.rounds)])
        )

    print("\n" + "=" * 68)
    print("SUMMARY (%s only - not a cross-platform result)" % platform.system())
    for ok in results:
        print("  %s" % ("PASS" if ok else "FAIL"))
    print("=" * 68)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
