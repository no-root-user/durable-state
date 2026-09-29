# FINDINGS

Bugs found in a working local-AI memory system, each one written as
**was / cause / now / proof**. The point of this file is not the count.

**Two of these were critical. Six were hygiene.** Splitting them is the point:
a list of eight equal-looking items is marketing, not engineering.

| # | Severity | Found by | Area |
|---|---|---|---|
| 1 | **CRITICAL** | self | atomic write |
| 2 | **CRITICAL** | outside reviewer | atomic write |
| 3 | minor | self | locks |
| 4 | minor | outside reviewer | locks |
| 5 | minor | self | session state |
| 6 | minor | self | session state |
| 7 | minor | outside reviewer | package integrity |
| 8 | minor | self | package integrity |
| 9 | **major, now fixed** | self | locks (liveness) |

Findings 1-9 are fixed and each carries its own proof. Finding 9 took the
longest: it was documented as "cause unknown" for a while, and that conclusion
turned out to rest on an instrument that was switched off during the very runs
it was supposed to explain. The retraction is kept in place, because a wrong
conclusion recorded honestly is more useful than a deletion.

---

## 1. CRITICAL - append destroyed any file over 2 MB

**WAS**
`atomic_append_text` guarded against loading a large file into memory:

```python
old = read_text() if size <= LIMIT else ""     # <- content discarded
atomic_write_text(target, old + line)
```

Every file above the limit was rewritten **empty**, then received the new line.

**CAUSE** The size check was written as a *content filter* when its intent was to
select a *strategy*. "Too big to read" was implemented as "nothing to keep".

**NOW** The threshold picks the method and never the content:
`in-memory` up to `APPEND_INLINE_MAX` (8 MB), otherwise `copy -> append ->
os.replace`. The strategy used is returned to the caller.

**PROOF** A 4.8 MB / 120 000-line file. Before: 25 bytes remained - all 120 000
lines destroyed, no exception raised. After: all 120 000 lines present plus the
new one. Pinned by `test_append_never_destroys_content`, parametrised at 1 000
and 120 000 lines so the strategy switch is covered on both sides.

**NOTE, honest one** Above the threshold the file is copied, so the operation
is O(size). The threshold exists to bound *memory*, and buys that with I/O.
For a multi-gigabyte store, use a database - that is what it is for.

---

## 2. CRITICAL - release() could delete a lock it no longer owned

**WAS** `release(name)` unlinked the lock file by name, unconditionally.

**CAUSE** No ownership check. If a lock was declared stale and taken over by
another process, the original owner's later `release()` deleted the *new* owner's
lock. The critical window: any holder that is slow, paused, or was suspended by
the OS between finishing and releasing.

**NOW** The owner writes a random UUID into the lock record, and `release()`
unlinks only when the token still matches. Additionally, a stale lock is moved
aside with `os.replace()` rather than deleted first, so two processes cannot both
believe they won the takeover.

**PROOF** `test_release_by_non_owner_is_refused` - a forged token must not
release someone else's lock. `test_release_twice_is_safe` - a second release is
a no-op, not a delete.

---

## 3. minor - fresh empty lock could be stolen

**WAS** An existing lock whose content could not be parsed was assumed stale.

**CAUSE** `O_EXCL` creates the file; the owner writes its identity a moment
later. A second process that reads an empty lock in that window concludes
"abandoned" and steals a lock that is being legitimately acquired.

**NOW** An empty lock younger than `EMPTY_GRACE` (1 s) is assumed alive. Older
ones are treated as crash debris.

**PROOF** `test_fresh_empty_lock_is_respected` (must wait), and
`test_old_empty_lock_is_stale` (must steal).

---

## 4. minor - lock identity was not durable

**WAS** The lock record was written but not flushed.

**CAUSE** Without `flush()` + `fsync()`, a crash could leave a lock file whose
identity bytes never reached disk - an empty lock with a live-looking name, which
is indistinguishable from debris and gets stolen by #3's logic.

**NOW** `flush()` then `os.fsync()` before the lock is considered acquired. If
the identity cannot be written, the lock is removed rather than left nameless.

**PROOF** `test_failed_write_keeps_original` (for the write path) plus the
`fsync` call itself; verified by inspection, not yet by a crash test - see
"Known gaps".

---

## 5. minor - `Path.read_text(newline=...)` breaks on Python < 3.13

**WAS** The in-memory append path called `Path.read_text(encoding=..., newline=...)`.

**CAUSE** `newline` was only added to `Path.read_text` in Python 3.13. On 3.12
and earlier this raises `TypeError` - so the *entire small-file path* would
break, which is the common case.

**NOW** Explicit `open(target, "r", encoding=..., newline="")`.

**PROOF** Found by `test_append_small_uses_inmemory`, not by reading. The
library claims 3.9+ support; this would have shipped broken on 3.12, the
version in use.

---

## 6. minor - CRLF translation made files machine-dependent

**WAS** Writes went through `newline=None`, so Python translated `\n` to
`\r\n` on Windows.

**CAUSE** Default text-mode behaviour, invisible on a single-machine setup and
loud the moment the same file is read on Linux, or hashed and compared across
platforms.

**NOW** `newline=""` on every write path, so the same bytes are produced on every
platform.

**PROOF** `test_newlines_are_not_translated` asserts exact bytes, and
`test_append_large_preserves_existing_bytes_exactly` uses deliberately mixed
endings to prove the copy path does not rewrite what is already there.

---

## 7. minor - a package with no integrity check

**WAS** The published package contained code with no manifest and no verifier.

**CAUSE** Nobody asked. A reviewer had instead reported that three files "looked
swapped" - which turned out to be a chat-side parsing artefact, not a real
defect, but it was still unprovable either way from inside the package.

**NOW** `checksums.sha256` plus a verifier that checks SHA-256 *and* strict
UTF-8 without BOM, and excludes binary files from the encoding check.

**PROOF** Two tests: a clean package exits 0, and a package with one modified
byte exits 1 and *names the file*. A checker that only ever says OK is not a
checker. The first run of that verifier also flagged its own `.mp4` as invalid
UTF-8, which is how the binary-exclusion rule got written.

---

## 8. minor - a secret lived in the source code

**WAS** A personal secret phrase was a hard-coded string in a module that was
about to be published.

**CAUSE** It was written for a private system and never revisited when the same
file became part of a public package. Pattern-based secret scanning does not
help: it looks for `ghp_` and `-----BEGIN`, not for something meaningful.

**NOW** The secret comes from the environment; the code holds no default value.
The lesson is the scan, not the fix: **secret scanners catch tokens, personal
data is caught by a word list.**

**PROOF** Repackaged and re-scanned: 0 hits for paths, names, or personal terms.

---

## 9. FIXED - a writer could deadlock on its own lock, and the instrumented build hid it

Found by this author's own stress testing, after an outside review pointed out
that no test ever had two processes writing the same file at once. That gap is
now closed, and closing it exposed this. It is now fixed, and the way it was
diagnosed is the more useful half of the story.

**WAS**

Under sustained contention, a process could end up unable to take a lock whose
file it did not delete, and block until its own timeout expired. The forensic
snapshot at the moment of failure:

```
mypid=5608   blames pid=5608   age=120.02s   state=held   pid_alive=True
start - created = -0.101s        (the process started BEFORE the lock)
self-deadlock: True
```

The blaming PID was the blocked process's **own** PID. Not a stranger, not a dead
holder: the process held a lock file it had created and could not remove it.

**CAUSE - Windows refuses to delete a file that anyone has open for reading**

`os.unlink()` on Windows raises `PermissionError` if any process holds a handle
to the file. It does not wait, and it does not say "someone is reading this". The
contender in `acquire()` does exactly that: `_is_stale()` calls `_read_owner()`,
which opens the lock file to read the owner's record, and holds the handle for
the duration of the read.

So the sequence was:

1. Process A holds the lock and calls `release()`.
2. Process B, deciding whether A's lock is stale, opens A's lock file to read it.
3. A's `os.unlink()` hits B's open handle and raises `PermissionError`.
4. The old `release()` caught that `OSError` and returned `False` - treating a
   microsecond read as a permanent condition.
5. The lock file stayed on disk **carrying A's own live PID**.
6. A's next `acquire()` read that PID, saw it was alive, concluded the lock was
   not stale, and blocked for the full timeout waiting for a lock that nobody
   else was holding.

That is the self-deadlock. The blame and the blocked PID were the same number
because they were literally the same process, looking at its own leaked file.

**THE MEASUREMENT THAT WAS WRONG, and why it was wrong**

An earlier version of this entry concluded: "instrumentation covered every exit
path of `release()`, and during wedged runs none of them fired, so no unlink
ever failed." That reasoning was invalid, and the flaw is worth more than the
bug.

`release.unlink_failed` only logs when `DURABLE_STATE_DEBUG` is set. The row
above reading "0 of 9 with `DURABLE_STATE_DEBUG` on" is the giveaway: **enabling
the logging made the bug disappear.** So the runs where "no path fired" were
runs where the logging was *not* enabled, and the absence of output was read as
evidence of absence of failure. A Heisenbug observed through an instrument that
was switched off on the runs that mattered. The conclusion "the file is not
being left behind by a failing `release()`" was exactly backwards, and it sent
the search after PID reuse instead of after the unlink.

A regression test now reproduces the old behaviour deterministically instead of
in one run in three, and it asserts the precondition first: while the reader's
handle is open, a plain `os.unlink()` of that path **must** raise. A pass can
therefore not come from the platform being generous.

**NOW**

`release()` retries a blocked delete for up to `UNLINK_GRACE` (1.0 s), polling
every 2 ms, and re-checks token ownership on every attempt so the retry cannot
weaken the "only the owner may delete this" rule. A blocker is a reader that
closes in microseconds, so 1.0 s is a million times the real window; a give-up
there means something is genuinely wrong, and `release()` reports it as
`False` rather than pretending it succeeded.

`FileNotFoundError` from the unlink is now treated as success - "gone" is the
state we were trying to reach, and a file that vanished between our ownership
check and our delete is not our problem.

**PROOF - same harness, old code vs fixed**

Identical 4-writer x 25-append stress, `timeout=120`, 24 rounds, run on the same
machine minutes apart:

| Build | Rounds with a wedge | Wall time of a wedged round |
|---|---|---|
| old `release()` | **10 of 24** | 120 s (the full timeout) |
| fixed `release()` | **0 of 24** | - |

With the fix, every one of the 24 rounds finished in ~0.5 s instead of hanging
for the timeout. The old build's 10/24 rate is higher than the 7/24 originally
recorded here, which is expected: rates move with machine load, and this run was
not trying to reproduce a specific historical number. What did not move is the
direction: 10 of 24 versus 0 of 24, on one harness, one variable changed.

Safety was asserted unconditionally in both builds and held throughout: no lost
lines, no duplicates, no mangled lines, no interior gaps in any wedged writer.
The bug was always a refusal, never corruption - and that is now the only thing
left to say about it, because it no longer happens.

**How much the two guards are worth, measured**

Neither guard is taken on faith, because the whole entry used to be wrong for
exactly that reason.

| Guard | Reverted to old code | Fixed code |
|---|---|---|
| `test_release_completes_while_a_reader_holds_the_file_open` (deterministic) | fails every time | passes |
| `test_concurrent_append_inmemory`, 12 runs | caught in 4 | caught in 0 |

The cross-process test is a wide net, not a reliable one: the race needs a
reader to be holding the lock file at the instant the owner deletes it, so it
finds the bug about a third of the time. The unit test forces that moment to
happen and fails every time. If this bug ever comes back in a way the unit test
does not catch, the cross-process test will probably notice within a few runs -
but "probably" is why the deterministic one is the one that carries the claim.

**WHAT TO DO IF YOU HIT IT ANYWAY**

- Still handle `LockBusy` and retry with backoff. It is a refusal, not a
  guarantee, and a library that can be interrupted should not assume it was not.
- **Do not raise your timeout to "fix" a stall.** Lengthening the timeout
  lengthens the block. A shorter timeout fails fast and the retry succeeds.

**STATUS:** fixed. `release()` no longer abandons a lock to a concurrent reader,
so a process can no longer find its own live PID in a lock it should be able to
take. Cause identified, mechanism reproduced by a test that fails on the old
code, and the previous "cause unknown" conclusion retracted.

---

## Known gaps - still open, not fixed

- **NFS is not safe for locking.** `O_EXCL` is not guaranteed atomic there. Not
  tested, not claimed. The module documents the boundary; a real fix means a
  different backend.
- **The verifier detects drift, not intent.** A hash tells you a file changed,
  not that the change was an improvement. Someone with write access can corrupt
  a file and re-sign the manifest.
- **Linux/macOS behaviour is asserted by CI, not yet confirmed by a run this
  author watched pass.** Until CI is green on those platforms, treat it as
  expected rather than demonstrated.
