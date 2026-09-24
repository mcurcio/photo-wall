# Bead 4: `rescue-lock-free`

**Design:** `docs/central-idempotent-jobs.md`: §6 ("Runtime"), §11 (defect 4).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1, which edits `tests/test_infra_job_queue.py`. It runs in parallel with beads 2
and 3, each in its own worktree; the file sets are disjoint.
**Packages:** `central/infra` (`job_queue.py`).

## Behaviour

Only asset fetch jobs hold procrastinate's one-runner lock. That lock saves a second download, and
correctness does not depend on it. Every other job type keeps its one-pending lock and may overlap
itself:
- **Rescue:** a stalled or paused rescue no longer blocks the next tick, which also rescues the
  stalled rescue row. This is procrastinate's documented pattern
  (`howto/production/retry_stalled_jobs`).
- **Sync:** an operator refresh during a running sync lists GitHub twice; the two converge by
  rule 2. That is the trade-off.
- **`Prefetch` and `Purge`:** they overlap harmlessly.

This replaces the round-0 `Delivery.concurrent` field, which had one user.

## Frozen page

- `central/infra/job_queue.py`: `_lock(job_type) -> str | None` returns `job_keys(job).lock` when
  `job_type.asset_kind is not None`, else None. Both `_register` (the periodic entry) and
  `_deferrer` (on-demand publishes) pass it. `queueing_lock` is unchanged everywhere.
- The module docstring says: "`lock` (one running copy) is taken only by asset fetches". The key
  itself is still the `job_outcomes` key and the NOTIFY payload.
- **No kernel change, no migration, no new error codes.**

## Files

- **Code:** `central/infra/job_queue.py`.
- **Tests:** `tests/test_infra_job_queue.py`, and `tests/test_rescue_lock_free.py` (new).

## Existing tests it carries

- `tests/test_infra_job_queue.py`:
  - the periodic entries' locks (about :155-166): `lock is None` for every periodic type;
  - the deferrer tests: an asset job keeps its lock, and a non-asset job has none.
- If any other file turns red (for example a conformance test that assumes a sync is joined
  while running), STOP and append an errata entry; do not edit files outside this set.

## Acceptance criteria (`tests/test_rescue_lock_free.py`, PostgreSQL with the procrastinate 3.9.0 schema)

1. **T3:**
   - Setup: a `RescueStalledJobs` row is `doing` under worker W1, whose `last_heartbeat` is an hour
     old; a `FetchOsImage` row is `doing` under W1 too; the next rescue tick is deferred.
   - Expect: W2's fetch returns that tick, and running it re-publishes both stalled jobs and closes
     their rows.
2. **P1 (a schema pin):**
   - Setup: a `doing` row and a `todo` row share one queueing lock.
   - Expect: `procrastinate_retry_job_v2(<doing id>, now(), NULL, NULL, NULL)` raises a unique
     violation on `procrastinate_jobs_queueing_lock_idx_v1`. This is why rescue re-publishes.
3. **Fetch keeps one runner:** with a `FetchPackage` row `doing`, its pending copy is not fetched.

## Mutation probes

- M1: restore the lock for every type (T3: W2's fetch returns nothing).
- M2: drop the lock for asset jobs too (test 3).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong. **Report where the spec is wrong.**
