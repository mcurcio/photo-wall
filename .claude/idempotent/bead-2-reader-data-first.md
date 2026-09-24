# Bead 2: `reader-data-first`

**Design:** `docs/central-idempotent-jobs.md`: rule 3, §4 (the Job outcome row and "Decision 3
still holds"), §5 note 2, §11 (defect 1 and N1).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1, which edits `tests/test_assets_reader.py`. It runs in parallel with beads 3
and 4, each in its own worktree; the file sets below are disjoint.
**Packages:** `central/assets` (the reader only).

## Behaviour

- Readiness is the data. The outcome row stays last-write-wins, and nothing about how outcomes are
  written changes.
- The reader already opens facts plus file before it publishes. After any wait, it now tries the
  data again before it reports an outcome.
- The bead also adds the missing test for issue #26's defect 1: a late `terminal` over present
  data changes nothing a Pi sees.

## Frozen page

`AssetReader._produce_and_open` (`central/assets/reader.py`):
1. After `handle.wait`, whatever the outcome, first call
   `_open_in_thread(self._open_first, (job,))`. Serve the file if it opens.
2. Otherwise, map the outcome exactly as today:
   - `Ready` becomes `absent_after_ready`;
   - `Failed` and `Pending` are unchanged.
3. Update the module docstring: "the data decides; an outcome only explains an absence".

There is no change to `outcomes.py`, `execution.py`, `publisher.py` or any Protocol. No migration.
No new codes.

## Files

- **Code:** `central/assets/reader.py`.
- **Tests:** `tests/test_assets_reader.py`, and `tests/test_reader_data_first.py` (new).
  - The new tests seed through `AssetRecords` and the cache disk only, never through
    `PublishedRelease` or `tests/content_db.py`'s release helpers, which bead 3 changes.

## Existing tests it carries

`tests/test_assets_reader.py`: any test that asserts a `Failed` or `Pending` result while the
asset's facts and file are present now gets the file. Keep every other expectation.

## Acceptance criteria (`tests/test_reader_data_first.py`, PostgreSQL, one `FetchPackage` key)

1. **D1 (defect 1):** a copy records `ok` and facts, and its file is on disk. A late
   `JobOutcomes.record(..., status="terminal")` then lands. Expect:
   - `reader.read` serves the file and publishes nothing;
   - `PrefetchHandler`'s missing set is empty.
2. **D2 (after the wait):** facts and file appear while the waiter waits, and the outcome it gets
   is `terminal`. Expect: the file is served.
3. **D3 (N1, stated):** facts are recorded, the file is removed, and the note is `terminal`.
   Expect:
   - a request publishes with `retry_terminal`, and one run is enqueued;
   - `Prefetch` publishes nothing.

## Mutation probes

- M1: the reader consults the outcome before `_open_first` in `read` (D1: a publish happens, and
  the result is a 503).
- M2: skip the post-wait data check (D2: 503 `terminal`).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong. **Report where the spec is wrong.**
