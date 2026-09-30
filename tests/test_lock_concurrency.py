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
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, %r)
from durable_state.locks import acquire, release, _lock_path

lockdir = sys.argv[1]
marker = Path(sys.argv[2])
cycles = int(sys.argv[3])
hold = float(sys.argv[4])
lockfile = _lock_path(Path(lockdir), "mx")
violations = 0
leaks = 0
evidence = []

def _read(p):
    try:
        return p.read_text(encoding="utf-8").strip().replace("\\n", " ")
    except OSError:
        return "unreadable"

def _marker_pid():
    try:
        return int(marker.read_text(encoding="utf-8").strip().split("=", 1)[1])
    except (OSError, ValueError, IndexError):
        return None

def _lock_pid():
    try:
        return int(json.loads(lockfile.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None

def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

for i in range(cycles):
    rec = acquire(lockdir, "mx", timeout=25)
    mine = False
    try:
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, ("pid=%%d" %% os.getpid()).encode("ascii"))
            os.close(fd)
            mine = True
        except FileExistsError:
            # "A marker exists" is not the invariant. The invariant is that no
            # two processes are inside at once, so what matters is whether the
            # marker belongs to somebody who is still holding the lock. A
            # marker left behind by a process that has already finished is
            # litter, not a violation - counting it is how this test spent
            # three CI runs blaming the lock for a cleanup problem, and
            # macOS failed 3 legs in a row for a reason the lock never
            # committed. Instrumentation showed every steal on macOS was
            # reason "gone": the lock had been released, so taking it was
            # correct. Not one live lock was ever stolen.
            mpid = _marker_pid()
            lpid = _lock_pid()
            if mpid is not None and mpid != lpid and not _alive(mpid):
                leaks += 1
                try:
                    marker.unlink()
                except OSError:
                    pass
            else:
                violations += 1
                evidence.append("me=%%d marker=%%s lock=%%s" %% (
                    os.getpid(), _read(marker), _read(lockfile)))
        time.sleep(hold)
    finally:
        # Only ever remove OUR marker. The unconditional unlink this replaced
        # let a process delete a live owner's marker, which breaks the very
        # invariant the test exists to check.
        if mine:
            try:
                marker.unlink()
            except OSError:
                pass
        release(lockdir, "mx", rec)
print("VIOLATIONS=%%d LEAKS=%%d" %% (violations, leaks))
for e in evidence:
    print("EVIDENCE " + e)
''' % (str(ROOT),)


def _child(tmp: Path) -> Path:
    p = tmp / "mx_child.py"
    p.write_text(CHILD_SRC, encoding="utf-8")
    return p


def _spawn(script: Path, lockdir: Path, marker: Path, cycles: int, hold: float, n: int):
    env = dict(os.environ)
    # Ask the lock to name every branch it uses to steal. Silent otherwise.
    env["DURABLE_STATE_DEBUG"] = "steal"
    return [
        subprocess.Popen(
            [sys.executable, str(script), str(lockdir), str(marker), str(cycles), str(hold)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
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
    evidence = []
    for k in kids:
        out, err = k.communicate(timeout=600)
        text = out.decode("utf-8", "replace")
        errtext = (err or b"").decode("utf-8", "replace")
        reported = re.search(r"VIOLATIONS=(\d+)", text)
        evidence.extend(
            line.strip() for line in text.splitlines() if line.startswith("EVIDENCE ")
        )
        steals = [
            line.strip() for line in errtext.splitlines() if "steal" in line and "DBG" in line
        ]
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
            evidence.extend(steals)
    assert not crashes, "a child died instead of reporting:\n%s" % "\n---\n".join(crashes)
    assert violations == 0, "a process saw two holders inside the lock:\n%s" % "\n".join(
        evidence
    )
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
