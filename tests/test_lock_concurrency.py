# Copyright (c) 2026 Durable State contributors | MIT License
"""
Cross-process lock tests. These exist because an external review pointed out
that concurrency was argued in prose, and because following that up found two
real defects that no unit test could see:

   1. acquire() raised FileNotFoundError when the holder released in the window
      between our failed O_EXCL and our staleness check. Confirmed by traceback
      at _is_stale(), fixed there.
   2. release() abandoned a lock whenever a contender was reading it, which on
      Windows is most of the time. The lock file survived its holder carrying
      that holder's own live PID, and the holder's next acquire() then blocked
      on it until its own timeout - the finding 9 self-deadlock. Reproduced
      deterministically in test_locks.py and fixed by retrying the delete.

Mutual exclusion is measured with a single shared marker file created with
O_CREAT|O_EXCL from inside the critical section. If the lock works, exactly one
process can create it. A per-process counter cannot detect this - it was the
mistake in the first version of this test.

    python -m pytest tests/test_lock_concurrency.py -v
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHILD_SRC = '''
import os, sys, time
from pathlib import Path
sys.path.insert(0, %r)
from durable_state.locks import acquire, release

lockdir = sys.argv[1]
marker = Path(sys.argv[2])
cycles = int(sys.argv[3])
hold = float(sys.argv[4])
violations = 0
for i in range(cycles):
    rec = acquire(lockdir, "mx", timeout=25)
    try:
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
        except FileExistsError:
            violations += 1
        time.sleep(hold)
    finally:
        # marker goes BEFORE the lock, so a legitimate hand-off cannot look
        # like a violation
        try:
            marker.unlink()
        except OSError:
            pass
        release(lockdir, "mx", rec)
print("VIOLATIONS=%%d" %% violations)
''' % (str(ROOT),)


def _child(tmp: Path) -> Path:
    p = tmp / "mx_child.py"
    p.write_text(CHILD_SRC, encoding="utf-8")
    return p


def _spawn(script: Path, lockdir: Path, marker: Path, cycles: int, hold: float, n: int):
    return [
        subprocess.Popen(
            [sys.executable, str(script), str(lockdir), str(marker), str(cycles), str(hold)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(n)
    ]


def test_mutual_exclusion_across_processes(tmp_path):
    """Only one process may be inside the critical section at a time."""
    script = _child(tmp_path)
    lockdir = tmp_path / "locks"
    lockdir.mkdir()
    marker = tmp_path / "INSIDE"

    kids = _spawn(script, lockdir, marker, cycles=8, hold=0.01, n=6)
    violations = 0
    crashes = []
    for k in kids:
        out, err = k.communicate(timeout=600)
        text = out.decode("utf-8", "replace")
        reported = re.search(r"VIOLATIONS=(\d+)", text)
        if reported is None:
            # A child that died never printed its count. The first version of
            # this test counted that as a mutual-exclusion violation and threw
            # the stderr away, so on macOS it reported "a process saw two
            # holders inside the lock" for a failure that had not been looked
            # at. An instrument that names the wrong cause is worse than no
            # instrument: it sends the reader to debug the lock instead of the
            # thing that actually broke.
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()
            crashes.append("exit=%s\n%s" % (k.returncode, "\n".join(tail[-6:])))
        elif int(reported.group(1)) > 0:
            violations += 1
    assert not crashes, "a child died instead of reporting:\n%s" % "\n---\n".join(crashes)
    assert violations == 0, "a process saw two holders inside the lock"
    assert not marker.exists(), "marker left behind"


def test_disappearing_lock_does_not_crash_acquire(tmp_path):
    """A lock that vanishes mid-inspection must be retried, not raise.

    Regression for the FileNotFoundError found under stress: the holder
    releases between our O_EXCL failure and _is_stale(), and the old code let
    that benign race escape as an exception.
    """
    script = _child(tmp_path)
    lockdir = tmp_path / "locks"
    lockdir.mkdir()
    marker = tmp_path / "INSIDE"

    # high churn, tiny hold time: maximises release/inspect overlap
    kids = _spawn(script, lockdir, marker, cycles=25, hold=0.001, n=6)

    crashes = []
    for k in kids:
        _out, err = k.communicate(timeout=600)
        if k.returncode != 0:
            crashes.append((err or b"").decode("utf-8", "replace"))
    filenotfound = [e for e in crashes if "FileNotFoundError" in e]
    assert not filenotfound, "acquire() still raises on a vanished lock:\n%s" % filenotfound[0][-500:]
    # LockBusy is the documented, safe failure: it refuses rather than
    # corrupting. So a timeout is acceptable here, a crash is not.
    assert not crashes or all("LockBusy" in e for e in crashes), "unexpected crash:\n%s" % crashes[0][-500:]


if __name__ == "__main__":
    raise SystemExit(0)
