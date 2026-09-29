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
| 9 | **known, unfixed** | self | locks (liveness) |

Findings 1-8 are fixed and each carries its own proof. Finding 9 is **not** fixed:
it is documented with its cause still unknown, because the evidence points at a
timing-dependent race that disappears when instrumented.

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

## 9. KNOWN, NOT FIXED - a writer can deadlock on its own lock

Found by this author's own stress testing, after an outside review pointed out
that no test ever had two processes writing the same file at once. That gap is
now closed, and closing it exposed this.

**WAS**

Under sustained contention, a process can end up unable to take a lock whose
file it did not delete, and block until its own timeout expires. Measured, not
imagined. The forensic snapshot at the moment of failure:

```
mypid=5608   blames pid=5608   age=120.02s   state=held   pid_alive=True
start - created = -0.101s        (the process started BEFORE the lock)
self-deadlock: True
```

The blaming PID is the blocked process's **own** PID. So this is not a stranger
holding a lock, and not a dead holder: the process holds a lock file it created
and cannot remove it.

**IMPACT - safety is intact, liveness is not**

- **No data impact.** Across every stress run: zero lost lines, zero duplicated
  lines, zero mangled lines. A refused writer never wrote, so it owes nothing.
  Whatever this bug does, it does not corrupt a file.
- **The failure is a refusal.** The writer raises `LockBusy` after its timeout.
  It never proceeds to write while the lock is held. That is the safe direction
  to fail in.
- **Writers that get in are never wrong.** The test asserts, per writer: one
  that finished has every line; one that was refused may hold a prefix, but
  never an interior gap, because an interior gap would mean a committed append
  vanished.

**RATE, as measured - configuration matters**

| Configuration | Wedge rate |
|---|---|
| 4 writers x 25 appends, `timeout=120` | 7 of 24 runs (~29%) |
| 6 writers x 25 appends, `timeout=45` | 0 of 9 |
| 4 writers, with `DURABLE_STATE_DEBUG` logging on | 0 of 9 |

Reproducing it needs a *long* wait. With a short timeout the writers give up
before the race develops. This matters when reading any workaround below.

**CAUSE - not identified, and the two obvious candidates are already ruled out**

`release()` and `unlink()` were instrumented on every exit path (missing file,
unreadable content, token mismatch, `unlink` failure). During wedged runs **none
of those paths fired**, and no unlink ever failed. So the lock is not being
left behind by a failing `release()`. The path that leaves the file is still
unknown. Adding the logging removed the bug entirely, which is what makes this
a heisenbug rather than a plain logic error.

Two candidate explanations were tested and **disproved**, recorded here so they
are not re-proposed later:

- *PID reuse.* Windows reuses PIDs. If a dead holder's PID had been taken over
  by a stranger, the blame would look identical. It does not happen here: the
  blaming process started **0.101 s before** the lock was written. A recycled
  PID would have started *after* it, so `start - created` would be positive.
  It is negative, which means the blame is genuine.
- *A half-written lock file.* If a reader caught the lock mid-write it would
  fail to parse, and `lock_info` would report `state="empty"`. Every wedged
  snapshot reported `state="held"` with a readable owner.

**WHAT TO DO IF YOU HIT IT**

- Handle `LockBusy` and retry with backoff. That is the whole remedy, because
  the condition is self-clearing once the stuck process exits.
- Keep the critical section short. The observed wedge grew with how long writers
  were queued.
- **Do not raise your timeout to "fix" it.** In every reproduction the timeout
  was long; lengthening it lengthens the block. A shorter timeout fails fast
  and the retry succeeds.

**STATUS:** documented, not fixed. Safety is demonstrated; liveness is not
guaranteed. This library promises it will not quietly lose or corrupt your data.
It does not promise that a writer always gets in.

---

## Known gaps - still open, not fixed

- **Lock liveness is not guaranteed.** See finding 9: under contention a writer
  can block on its own lock until its timeout, then fail with `LockBusy`. No
  data is lost; the write simply does not happen. Cause unknown.
- **NFS is not safe for locking.** `O_EXCL` is not guaranteed atomic there. Not
  tested, not claimed. The module documents the boundary; a real fix means a
  different backend.
- **The verifier detects drift, not intent.** A hash tells you a file changed,
  not that the change was an improvement. Someone with write access can corrupt
  a file and re-sign the manifest.
- **Linux/macOS behaviour is asserted by CI, not yet confirmed by a run this
  author watched pass.** Until CI is green on those platforms, treat it as
  expected rather than demonstrated.
