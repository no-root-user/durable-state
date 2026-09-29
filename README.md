# durable-state

Crash-safe writes, single-node locks, and integrity verification for local AI
memory files. Zero dependencies, Python 3.9+.

A local AI agent's memory is usually a pile of JSON on disk. `json.dump(...)`,
`write_text(...)`, a scheduled autosave, two agents running at once - and one
day the memory file is empty, half-written, or silently truncated. The agent
does not crash. It does not warn. It just hallucinates confidently over a
corrupted brain.

This library makes those three failures impossible to miss, and two of them
impossible to commit.

```
pip install "durable-state @ git+https://github.com/no-root-user/durable-state.git"
```

That is the whole install story, and it is worth being precise about why: **this
is not on PyPI.** There is no `pip install durable-state` and there is no wheel
to download. If a command you found elsewhere says otherwise, that page is
wrong - or describes a package that is not this one. Installing from GitHub is
the supported path until a release is published to an index.

No runtime dependencies. Python 3.9+.

## What it does

| Module | Problem it removes |
|---|---|
| `atomicio` | `write_text()` is not atomic. A crash mid-write leaves an empty or half file. |
| `locks` | Two writers on one memory file. Plus deadlock, plus two subtle lock races. |
| `verify` | "Did my files arrive intact, and are they the encoding I think?" |

```python
from durable_state import atomic_write_json, FileLock, verify_dir

with FileLock(lock_dir, "memory_graph"):
    atomic_write_json(path / "memory_graph.json", state)   # never half-written

if not verify_dir(path)["ok"]:
    raise SystemExit("memory store damaged - refusing to continue")
```

## The two bugs that mattered

**1. `atomic_append_text` destroyed every file over 2 MB.** The original guard
against loading a huge file into memory read:

```python
old = read_text() if size <= LIMIT else ""    # data silently discarded
atomic_write_text(target, old + line)
```

Any file above the limit was rewritten **empty**, then got the new line. A
4.8 MB / 120 000-line memory file became 25 bytes. No exception, no log, no
backup. The size check was meant to *choose a strategy*; it had been written as
a *content filter*.

Fixed by making the threshold choose the strategy and never the content: below
8 MB, rewrite in memory; above, copy to a temp file in the same directory,
append to the copy, then `os.replace()`.

**2. `release()` could delete a lock it no longer owned.** A lock file records
its owner. If a lock was considered stale and taken over by another process, the
original owner would later call `release()` and unlink *by name* - deleting the
new owner's lock. Two writers, one lock, no protection.

Fixed with an ownership token: the owner writes a random UUID into the lock, and
`release()` only unlinks when the token still matches. There is a test for this
specifically (`test_release_by_non_owner_is_refused`).

Both bugs were found by an outside reviewer reading a debug package, not by
inspection. That is in `FINDINGS.md` with the full list.

## Scope and limits, stated plainly

- **Single node, local filesystem only.** The lock uses `O_EXCL`, which is *not*
  guaranteed atomic on NFS. Do not use it across machines over a network
  filesystem; use a real network lock service there. This is a hard boundary,
  not a caveat.
- **Still handle `LockBusy` and retry.** The self-deadlock in finding 9 - a
  writer blocking on a lock file it had created itself - is fixed at the source:
  `release()` no longer abandons a lock to a concurrent reader. On one stress
  harness that went from 10 wedged runs in 24 to 0 in 24. But `LockBusy` is
  still a refusal, not a promise, and code that can be interrupted should not
  assume it was never interrupted. **Do not raise your timeout to "fix" a
  stall** - a longer timeout blocks for longer. Details in `FINDINGS.md`,
  finding 9.
- **Lock staleness across hosts falls back to age.** The module will not trust
  a PID check for a host it cannot see, so a lock from another machine is only
  reclaimed after `STALE_PID_AGE` (300 s).
- **`atomic_append_text` above 8 MB is O(size)** - it copies the file. The
  threshold bounds peak memory at the cost of I/O. The strategy used is
  returned, so you can log it.
- **Verified on Windows (NTFS), and honestly nowhere else yet.** The suite has
  been run on Windows only. The CI workflow that would cover Linux and macOS is
  written but not yet running, so this project does not claim those platforms.
  See "CI status" below.
- **The verifier detects drift, not intent.** A hash tells you a file changed,
  not that a change was good.

## Verification is part of the product

```
python -m durable_state.verify <dir>     # exit 0 = intact, 1 = damaged
```

Checks SHA-256 against a manifest *and* strict UTF-8 without BOM. Binary files
are checked by hash only - judging an `.mp4` as text produces nonsense, which is
exactly the kind of thing that passes review until someone trips over it.

`tests/crash_harness.py` goes further: it kills a real writer process outright
at randomised moments and asserts a reader never observes a partial file. It
includes a positive control, so "all rounds passed" cannot mean "the kill
window was too tight to matter". The harness prints which kill it used, because
`SIGKILL` does not exist on Windows: POSIX gets `os.kill(pid, SIGKILL)`, Windows
gets `TerminateProcess`. Both are uncatchable, which is the point.

## Development

```
pip install -e ".[test]"
python -m pytest tests -v
python tests/crash_harness.py --rounds 40
```

### Before you publish or ship: audit yourself

```
python tools/audit_release.py      # personal paths, secrets, mojibake, stale manifest
python tools/audit_selftest.py     # proves the audit can still fail
python tools/make_manifest.py dist/ # generate checksums for what you ship
python tools/local_ci.py           # tests + crash harness
```

`audit_release.py` is not decoration. It exists because this repository was one
commit from being published with two defects a visitor would have found
immediately:

1. `tools/check_drift.py` - the leak detector - had hardcoded private
   filesystem paths baked in. The auditor was itself the leak.
2. `checksums.sha256` was committed, so it went stale on the very next commit
   and then reported a match that was not there. An integrity tool that lies is
   worse than none.

And then the audit's own path regex was found to be dead code: it required a
doubled backslash, so it could never match a real Windows path. The single most
important check in the file had been passing vacuously. That is why
`audit_selftest.py` exists: every detector must be shown firing on a known-bad
sample before its "clean" verdict means anything.

The self-test holds deliberately bad samples, so it declares itself a fixture
(`audit-fixture:`) and the audit skips it - explicitly, and prints the exclusion
rather than hiding it.

### CI status

`.github/workflows/ci.yml` is present in the working tree and is **not pushed**:
the deploy token in use lacks GitHub's `workflow` scope, and GitHub refuses
workflow files from tokens without it. The file has not been renamed or shimmed
to get around the restriction.

Until that changes, `tools/local_ci.py` runs the same suite locally, and it
prints `SUMMARY (Windows only - not a cross-platform result)` - it will not
claim anything about platforms it has not run on. Nothing in this README claims
Linux or macOS are demonstrated; see FINDINGS, "Known gaps".

MIT. Take it, change it, sell it - keep the license text.
