# Copyright (c) 2026 Durable State contributors | MIT License
"""
Cross-process append test.

An external review of this library pointed out a real gap: concurrent appends
were argued about in prose and in unit tests, but no test actually had two
processes writing the same file at the same time. Since append is the function
that had the critical bug, "unit-level reasoning" is not good enough.

So: spawn N real processes, let them fight over one file, and check afterwards
that every single line survived exactly once. No mocks, no threads.

Both strategies are exercised. APPEND_INLINE_MAX is patched down in the child,
because _append_strategy() reads the module global at call time - so the
expensive copy-then-append path can be forced on a tiny file instead of needing
an 8 MB fixture.

Usage:
    python tests/test_concurrent.py --child TARGET LOCKDIR TAG N THRESHOLD
    python -m pytest tests/test_concurrent.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Filled in whenever a writer is refused by the known lock-liveness defect, so
# the suite can report the rate instead of losing the observation.
WEDGE_SEEN: list = []
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from durable_state import atomicio  # noqa: E402
from durable_state.atomicio import atomic_append_text  # noqa: E402
from durable_state.locks import FileLock, LockBusy  # noqa: E402

LOCK_NAME = "concurrent-append"


def _wedge_snapshot(lockdir: str, exc: BaseException) -> str:
    """Forensics for a stuck writer: WHO is the lock file blaming, and are they real?

    Two very different causes look identical from the caller's side, so the
    distinction has to be measured, not assumed:
      - the blamed process is genuinely alive  -> the bug is in release/steal;
      - the blamed process is a stranger that merely inherited a recycled PID
        -> our liveness check is wrong and must compare process start time.
    The second case is a ghost lock: nothing holds it, yet every writer waits.
    """
    import ctypes
    import json
    from ctypes import wintypes

    from durable_state.locks import lock_info

    out = {"event": "wedge", "mypid": os.getpid(), "exc": type(exc).__name__}
    try:
        # lock_info takes (locks_dir, name) - passing a full file path silently
        # returns an empty owner, which is how this harness first produced a
        # convincing, completely fictitious "ghost lock".
        info = lock_info(lockdir, LOCK_NAME)
    except Exception as inner:  # never let forensics mask the real failure
        out["info_error"] = repr(inner)
        return json.dumps(out)
    out["info"] = info
    out["age"] = info.get("age")
    out["pid_alive"] = info.get("pid_alive")
    out["state"] = info.get("state")
    # lock_info is FLAT: pid/host/created sit at the top level, there is no
    # "owner" sub-dict. Reading info["owner"] is a silent way to invent ghosts.
    opid = info.get("pid")
    out["owner_pid"] = opid
    if not isinstance(opid, int):
        out["probe_skip"] = "no usable pid (state=%s)" % info.get("state")
        return json.dumps(out)
    out["owner_created"] = info.get("created")
    out["owner_image"] = "<unknown>"
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, opid)  # QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf = ctypes.create_unicode_buffer(260)
                n = wintypes.DWORD(260)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    out["owner_image"] = buf.value
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                       ctypes.byref(k), ctypes.byref(u)):
                    ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
                    started = ticks / 1e7 - 11644473600.0
                    out["owner_start"] = started
                    created = info.get("created")
                    if isinstance(created, (int, float)):
                        out["start_minus_lockcreated"] = round(started - created, 3)
                        out["ghost_lock"] = started > created
            finally:
                k32.CloseHandle(h)
    except Exception as inner:
        out["probe_error"] = repr(inner)
    return json.dumps(out)


def child(target: str, lockdir: str, tag: str, count: int, threshold: str, timeout: float = 120.0) -> int:
    """One writer process. Appends `count` uniquely tagged lines under a lock.

    `timeout` exists so the forensics harness can fail fast. A wedged writer
    blocks for exactly this long, so a 120 s default would turn a 20 s
    investigation into an hour of waiting.
    """
    count = int(count)  # argv gives us strings
    if threshold and threshold != "default":
        # forces APPEND_STRATEGY_COPY for every append
        atomicio.APPEND_INLINE_MAX = int(threshold)
    for i in range(count):
        try:
            with FileLock(lockdir, LOCK_NAME, timeout=float(timeout)):
                atomic_append_text(target, "%s %04d\n" % (tag, i))
        except LockBusy as exc:
            print(_wedge_snapshot(lockdir, exc), file=sys.stderr, flush=True)
            return 3
    return 0


def _run(tmp: Path, procs: int, per_proc: int, threshold: str) -> dict:
    target = tmp / "mem.txt"
    lockdir = tmp / "locks"
    target.write_text("", encoding="utf-8")

    kids = [
        subprocess.Popen(
            [
                sys.executable,
                str(HERE / "test_concurrent.py"),
                "--child",
                str(target),
                str(lockdir),
                "p%d" % p,
                str(per_proc),
                threshold,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for p in range(procs)
    ]

    # Wait for every child BEFORE reading the file. Reading earlier looks like
    # total data loss and is not.
    failures = []
    busy = []
    for p, kid in enumerate(kids):
        out, err = kid.communicate(timeout=600)
        if kid.returncode != 0:
            text = (err or b"").decode("utf-8", "replace")
            if "LockBusy" in text:
                busy.append(p)  # documented wedge, see below
            else:
                failures.append("child p%d exited %d\n%s" % (p, kid.returncode, text[-400:]))
    assert not failures, "writer processes failed:\n" + "\n".join(failures)

    text = target.read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln]

    # SAFETY, asserted unconditionally: whatever the wedge did, it must never
    # have corrupted or duplicated a line. Every line is intact and parseable.
    seen = set()
    for ln in lines:
        parts = ln.split()
        assert len(parts) == 2, "mangled line: %r" % ln
        tag, idx = parts
        assert tag.startswith("p") and tag[1:].isdigit(), "bad tag: %r" % ln
        assert 0 <= int(idx) < per_proc, "out-of-range index: %r" % ln
        key = (tag, int(idx))
        assert key not in seen, "DUPLICATE line: %r" % ln
        seen.add(key)

    leftovers = [q.name for q in tmp.iterdir() if q.name not in ("mem.txt", "locks")]
    assert not leftovers, "temp files left behind: %s" % leftovers

    # COMPLETENESS, per writer. A writer that ended in LockBusy is NOT a writer
    # that wrote nothing: it typically got the lock, committed a prefix, and
    # only then wedged on its own leaked lock. Observed, not assumed. So:
    #   - a writer that finished cleanly must have every one of its lines;
    #   - a wedged writer may hold a prefix, but it must have no interior gap
    #     (that would mean a committed append vanished, which is corruption).
    for p in range(procs):
        got = sorted(i for (t, i) in seen if t == "p%d" % p)
        if p in busy:
            assert got == list(range(len(got))), (
                "wedged writer p%d has an interior gap %s - committed appends vanished"
                % (p, got)
            )
        else:
            assert got == list(range(per_proc)), (
                "writer p%d finished but is missing lines: %s"
                % (p, [i for i in range(per_proc) if i not in got])
            )
    return {"lines": len(lines), "busy": busy}


def _report_wedge(res: dict, record_property) -> None:
    """A writer refused with LockBusy is now a FAILURE, not a documented defect.

    This used to record the refusal and move on, because the self-deadlock in
    finding 9 was known, unexplained, and could not be made to reproduce on
    demand. It has since been fixed at the source: `release()` no longer gives
    up a lock because another process happened to be reading it, so a writer
    cannot end up blocking on its own leaked file.

    That changes the contract of this test. Previously safety was asserted and
    liveness was merely observed. Now both are asserted, because a wedge here
    means the fix regressed - and a regression that this file is allowed to
    describe in a docstring is a regression nobody has to act on.

    `timeout` is what makes a regression expensive to miss: a wedge costs the
    full timeout, so a reappearance shows up as a slow, obvious failure rather
    than a quiet one.

    The refusal is still recorded in the test report, so if it ever comes back
    the evidence is in the log rather than in someone's memory.

    HOW MUCH THIS IS WORTH, measured rather than hoped for: running this test
    12 times against the reverted (buggy) release() catches the regression 4
    times, and 0 times against the fix. The race needs a reader to hold the
    lock file open at the exact moment the owner deletes it, so one green run
    proves nothing - here, as in finding 9, the instrument is only as good as
    the runs that actually exercised it.

    The deterministic guard is in test_locks.py
    (test_release_completes_while_a_reader_holds_the_file_open), which forces
    the blocking read to happen and fails every time on the old code. This test
    is the wide net for the same bug, not the net that can be relied on.
    """
    record_property("wedge_writers", ",".join(str(p) for p in res["busy"]))
    if res["busy"]:
        WEDGE_SEEN.append(res["busy"])
        assert not res["busy"], (
            "%d writer(s) were refused with LockBusy on a lock that leaked past "
            "its holder. That is the finding 9 self-deadlock returning; the fix "
            "in release() has regressed. Safety assertions above still held - "
            "the data is intact - but writers are being turned away. See "
            "FINDINGS.md, finding 9." % len(res["busy"])
        )


def test_concurrent_append_inmemory(tmp_path, record_property):
    """Default path: rewrite in memory. Several processes, one file."""
    res = _run(tmp_path, procs=4, per_proc=25, threshold="default")
    _report_wedge(res, record_property)


def test_concurrent_append_copy_strategy(tmp_path, record_property):
    """Forced expensive path: copy, append to the copy, replace. Same guarantee."""
    res = _run(tmp_path, procs=4, per_proc=15, threshold="0")
    _report_wedge(res, record_property)


if __name__ == "__main__":
    if "--child" in sys.argv:
        a = sys.argv.index("--child")
        raise SystemExit(child(*sys.argv[a + 1 :]))
    raise SystemExit(0)

