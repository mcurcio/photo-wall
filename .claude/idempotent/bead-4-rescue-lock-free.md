# Bead 4: `rescue-lock-free`

**Design:** `docs/central-idempotent-jobs.md`, §6 (the runtime table), §11 (defect 4).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1 (it shares `central/kernel/job_types.py` and the kernel and queue test files).
It runs in parallel with beads 2 and 3, each in its own worktree; the files are disjoint.
**Packages:** `central/kernel`, `central/infra` (`job_queue.py`).

## Behaviour

Rescue keeps its one-pending lock but takes no one-runner lock. A rescue that is stalled or
paused can no longer block the next tick, and the next tick also rescues the stalled rescue row.
This is procrastinate's documented stalled-jobs pattern (`howto/production/retry_stalled_jobs`).
Every other job type keeps its one-runner lock, as a saving of work, not for correctness.

## Frozen page

- `Delivery` (`central/kernel/jobs.py`) gains `concurrent: bool = False`, meaning "runs
  concurrently with itself: no one-runner lock".
  - `__post_init__` raises `ValueError("invalid_concurrent")` unless it is exactly a `bool`.
  - `_check_asset` raises `TypeError` for an asset job type with `concurrent=True`: a download
    keeps one runner.
- `JobKeys.lock` gets a docstring: "the job's key: always the `job_outcomes` key and the NOTIFY
  payload; also procrastinate's one-runner lock unless the type is concurrent".
- `RescueStalledJobs` (`central/kernel/job_types.py`) declares
  `Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=1), concurrent=True)`.
- `central/infra/job_queue.py` passes `lock=None` for a concurrent type, and always keeps
  `queueing_lock`. This applies to both `_register` (the periodic entry) and `_deferrer`
  (on-demand publishes).
- **No migration and no new error codes.**

## Files

- **Code:** `central/kernel/jobs.py`, `central/kernel/job_types.py`, `central/infra/job_queue.py`.
- **Tests:** `tests/test_kernel_jobs.py`, `tests/test_infra_job_queue.py`,
  `tests/test_rescue_lock_free.py` (new).

## Existing tests it carries

- `tests/test_kernel_jobs.py` (the `Delivery` bounds, about :286-293): add the `concurrent`
  validation and the asset refusal.
- `tests/test_infra_job_queue.py` (the periodic entries' locks, about :155-166): `lock is None` for
  `RescueStalledJobs`, and the key for every other periodic type.

## Acceptance criteria (`tests/test_rescue_lock_free.py`, PostgreSQL with the procrastinate 3.9.0 schema)

1. **T3:** a `RescueStalledJobs` row is `doing` under worker W1, whose `last_heartbeat` is an
   hour old, and a `FetchOsImage` row is also `doing` under W1. Defer the next rescue tick.
   Expect: worker W2's fetch returns that tick, and running it re-publishes both stalled jobs and
   closes their rows.
2. **P1 (a schema pin):** a `doing` row and a `todo` row share one queueing lock. Expect:
   `procrastinate_retry_job_v2(<doing id>, now(), NULL, NULL, NULL)` raises a unique violation on
   `procrastinate_jobs_queueing_lock_idx_v1`. This pins why rescue re-publishes instead of
   retrying in place.
3. **Overlap:** two rescues run at once over the same stalled row. Expect one pending copy, and
   at most one `rescue_incomplete`.

## Mutation probes

- M1: restore the one-runner lock on `RescueStalledJobs` (T3: W2's fetch returns nothing).
- M2: accept `concurrent=True` on an asset job type (the kernel test).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong. **Report where the spec is wrong.**
