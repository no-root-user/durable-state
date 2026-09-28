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

## Known gaps - still open, not fixed

- **NFS is not safe for locking.** `O_EXCL` is not guaranteed atomic there. Not
  tested, not claimed. The module documents the boundary; a real fix means a
  different backend.
- **No cross-process stress test for the append path.** The crash harness covers
  writes; concurrent appends are covered only by unit-level reasoning.
- **The verifier detects drift, not intent.** A hash tells you a file changed,
  not that the change was an improvement. Someone with write access can corrupt
  a file and re-sign the manifest.
- **Linux/macOS behaviour is asserted by CI, not yet confirmed by a run this
  author watched pass.** Until CI is green on those platforms, treat it as
  expected rather than demonstrated.
