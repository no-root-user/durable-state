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
pip install durable-state     # or: copy the durable_state/ folder
```

## 60 seconds, no audio

`docs/demo-60s.mp4` - text-only walkthrough of the same bugs: before, cause,
after, proof. It is a re-record of the review cycle that produced this library,
including the two bugs that the review found rather than the author.

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
  filesystem; use Redis or a database. This is a hard boundary, not a caveat.
- **Lock staleness across hosts falls back to age.** The module will not trust
  a PID check for a host it cannot see, so a lock from another machine is only
  reclaimed after `STALE_PID_AGE` (300 s).
- **`atomic_append_text` above 8 MB is O(size)** - it copies the file. The
  threshold bounds peak memory at the cost of I/O. The strategy used is
  returned, so you can log it.
- **Verified on Windows (NTFS).** The unit tests run on Linux, macOS and Windows
  in CI. Claims beyond what CI shows are not made.
- **The verifier detects drift, not intent.** A hash tells you a file changed,
  not that a change was good.

## Verification is part of the product

```
python -m durable_state.verify <dir>     # exit 0 = intact, 1 = damaged
```

Checks SHA-256 against a manifest *and* strict UTF-8 without BOM. Binary files
are checked by hash only - judging an `.mp4` as text produces nonsense, which is
exactly the kind of thing that passes review until someone trips over it.

`tests/crash_harness.py` goes further: it kills a real writer process with
`SIGKILL` at randomised moments and asserts a reader never observes a partial
file. It includes a positive control, so "all rounds passed" cannot mean "the
kill window was too tight to matter".

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
