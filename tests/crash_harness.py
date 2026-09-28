# Copyright (c) 2026 Vika Integrity contributors | MIT License
"""
Crash harness: kill a writer mid-write and prove readers never see garbage.

The unit tests simulate a failed write by making os.replace() raise. That is a
mock. This harness uses the real thing: a child process is SIGKILLed at a random
moment while it is writing, and the parent then inspects the target file.

What must hold after every kill, without exception:
  1. The target is EITHER the old content OR the complete new content.
  2. The target is never empty, never truncated, never a mix.
  3. Leftover temp files are allowed (the process died before cleanup) but the
     target must always be readable and parseable.

Usage:  python tests/crash_harness.py --rounds 40
Exit 0 = invariant held in every round.
"""
from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vika_integrity.atomicio import atomic_write_text  # noqa: E402

OLD = "OLD-CONTENT\n" * 2000
NEW = "NEW-CONTENT\n" * 40000  # ~500 KB: long enough to be killed mid-write


def writer(target: Path) -> None:
    atomic_write_text(target, NEW)
    os._exit(0)


def classify(text: str) -> str:
    if text == OLD:
        return "old"
    if text == NEW:
        return "new"
    return "CORRUPT"


def spawn(target: Path) -> int | subprocess.Popen:
    """Start a writer. Returns a pid on POSIX or a Popen on Windows."""
    if hasattr(os, "fork"):
        pid = os.fork()
        if pid == 0:
            writer(target)
        return pid
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--child", str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def timed_run(target: Path) -> float:
    """How long does an uninterrupted write actually take? Includes interpreter
    startup on the subprocess path, which dominates on Windows."""
    t0 = time.time()
    proc = spawn(target)
    if isinstance(proc, int):
        os.waitpid(proc, 0)
    else:
        proc.wait()
    return max(time.time() - t0, 0.001)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--child", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        writer(Path(args.child))
        os._exit(0)

    work = Path(args.workdir) if args.workdir else Path(__file__).resolve().parent.parent / ".crash"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "store.txt"
    atomic_write_text(target, OLD)

    random.seed(1234)  # reproducible failures
    results = {"old": 0, "new": 0, "CORRUPT": 0}
    leftovers = 0

    # POSITIVE CONTROL. Without this the harness is worthless: if the child
    # never finished a single write, every round would report "old" and the
    # test would pass while proving nothing.
    atomic_write_text(target, OLD)
    assert classify(target.read_text(encoding="utf-8")) == "old", "reset failed"
    atomic_write_text(target, NEW)
    assert classify(target.read_text(encoding="utf-8")) == "new", "positive control failed"
    print("positive control: an uninterrupted write is observed as 'new'")

    # Calibrate the kill window against a measured run. A fixed 20 ms window
    # always killed the child during interpreter startup, so the write never
    # began and the test proved nothing.
    duration = timed_run(target)
    print("calibration: one full write takes %.3f s" % duration)
    for i in range(1, args.rounds + 1):
        atomic_write_text(target, OLD)  # reset to a known-good state
        # Sample across the whole run: 0.0..1.15x duration, so some rounds are
        # killed before the write and some after it completed.
        proc = spawn(target)
        time.sleep(duration * random.uniform(0.0, 1.15))
        if isinstance(proc, int):
            try:
                os.kill(proc, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(proc, 0)
        else:
            proc.kill()
            proc.wait()

        try:
            got = classify(target.read_text(encoding="utf-8"))
        except OSError as e:
            print("  round %d: target unreadable: %s" % (i, e))
            return 1
        results[got] += 1

        temps = [p.name for p in work.iterdir() if p.name != "store.txt"]
        if temps:
            leftovers += 1
            for t in temps:
                try:
                    (work / t).unlink()
                except OSError:
                    pass

        if got == "CORRUPT":
            print("  FAIL round %d: reader saw a partial file (%d bytes)" % (i, target.stat().st_size))
            return 1

    print("rounds: %d | old: %d | new: %d | corrupt: %d | rounds with temp leftovers: %d"
          % (args.rounds, results["old"], results["new"], results["CORRUPT"], leftovers))
    if results["new"] == 0:
        print("WARNING: no round observed a completed write - the kill window is too")
        print("         tight, so this run is weaker than it looks. Not a failure,")
        print("         but do not report it as strong evidence.")
    if results["old"] + results["new"] < args.rounds:
        return 1
    print("INVARIANT HELD: a reader never observed a partially written file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
