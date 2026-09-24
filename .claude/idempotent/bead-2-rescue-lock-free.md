# Bead 2: `rescue-lock-free`

**Design:** `docs/central-idempotent-jobs.md`, rule 2, §6 table and §11 (defect 4).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1. They share `tests/test_infra_job_queue.py`, so build in bead 1's worktree after it lands.
**Packages:** `central/kernel` and `central/infra`.

## Behaviour

Rescue keeps its queueing lock (one pending copy) but takes no running lock. A rescue that is
stalled or paused can no longer block the next tick, and the next tick rescues the stalled rescue
row too. This is procrastinate's documented stalled-jobs pattern (`howto/production/retry_stalled_jobs`).

## Frozen page

**Signatures**
- `Delivery` (`central/kernel/jobs.py`) gains `concurrent: bool = False`, meaning "may run
  concurrently with itself: no running lock".
  - `__post_init__` raises `ValueError("invalid_concurrent")` unless the value is exactly a `bool`.
  - `_check_asset` raises `TypeError` for an asset job type with `concurrent=True`, because a
    download keeps one runner.
- `JobKeys.lock` gets a docstring and comment: "the job's key: always the `job_outcomes` key and
  the NOTIFY payload; also procrastinate's running lock unless the type is concurrent".
- `RescueStalledJobs` (`central/kernel/job_types.py`) declares `Delivery(queue=QueueName.UPKEEP,
  every=timedelta(minutes=1), concurrent=True)`.
- `central/infra/job_queue.py` passes `lock=None` for a concurrent type, and always keeps
  `queueing_lock`:
  - `_register` for the periodic entry;
  - `_deferrer` for on-demand publishes.

**No migration, no new error codes.**

## Files

- **Code:** `central/kernel/jobs.py`, `central/kernel/job_types.py`, `central/infra/job_queue.py`.
- **Tests:** `tests/test_kernel_jobs.py`, `tests/test_infra_job_queue.py`, and
  `tests/test_rescue_lock_free.py` (new).

## Existing tests it carries

- `tests/test_kernel_jobs.py:286-293`: the `Delivery` bounds. Add `concurrent` validation and the
  asset refusal.
- `tests/test_infra_job_queue.py:155-166`: the periodic entries' locks. Expect `lock is None` for
  `RescueStalledJobs` and the key for every other periodic type.

## Acceptance criteria (`tests/test_rescue_lock_free.py`, PostgreSQL with the procrastinate 3.9.0 schema)

1. **T3:**
   - Setup: a `RescueStalledJobs` row is `doing` under worker W1, whose `last_heartbeat` is 1h old,
     and a `FetchOsImage` row is `doing` under W1 too. The next rescue tick is deferred.
   - Then: worker W2's fetch returns that tick, and running it re-publishes both stalled jobs and
     closes their rows.
2. **P1** (schema pin):
   - Setup: a `doing` row and a `todo` row share one queueing lock.
   - Expect: `procrastinate_retry_job_v2(<doing id>, now(), NULL, NULL, NULL)` raises a unique
     violation on `procrastinate_jobs_queueing_lock_idx_v1`.
   - This pins why rescue re-publishes instead of retrying in place.

## Mutation probes

- M1: restore the running lock on `RescueStalledJobs` (T3 red: W2's fetch returns nothing).
- M2: accept `concurrent=True` on an asset job type (the kernel test red).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong.
