# Bead 1: `outcome-start-order` (the tracer)

**Design:** `docs/central-idempotent-jobs.md`: rule 1, §4 to §6 and §8. Read those sections only.
**Base branch:** `claude/central-followups`. **Follows:** none (first bead).
**Followed by:** bead 2 (same worktree, shares a test file) and bead 4 (needs this behaviour).
**Packages:** `central/infra` and `central/migrations` (plus one wiring line in `central/`).

## Behaviour

A run draws a start number when it starts. Its result is stored only if no run that started later
has stored one. A refused result rolls back its produced facts in the same transaction, sends no
NOTIFY, and schedules no redelivery. Worker transactions are killed after 2s idle, so a paused run
frees the key's locks.

## Frozen page

**Migration `central/migrations/028_outcome_start_number.sql`**
```
CREATE SEQUENCE job_start_seq CACHE 1;
ALTER TABLE job_outcomes ADD COLUMN start_number BIGINT NOT NULL DEFAULT 0;
```
- Existing rows lose to any new run.
- Rollback is a code revert. Old code names its columns, so it ignores this one.

**Signatures**
- `OutcomeRow` (`central/infra/outcomes.py`) gains `start_number: int`, which also goes into
  `_COLUMNS`.
- `JobOutcomes.start(self, tx: Transaction) -> int` runs `SELECT nextval('job_start_seq')`.
- `JobOutcomes.record(self, tx, job, *, status, reason, retry_not_before, now, start_number: int)
  -> OutcomeRow | None`:
  - It runs `INSERT … ON CONFLICT (lock_key) DO UPDATE SET …, start_number =
    EXCLUDED.start_number WHERE job_outcomes.start_number <= EXCLUDED.start_number RETURNING …`.
  - `pg_notify` is called ONLY when a row came back.
  - `None` means refused: a later-started run has stored a result.
- `OutcomeWriter` (the Protocol in `central/infra/execution.py`) mirrors `start` and the new
  `record`.
- `JobExecutor.execute(self, job, attempt) -> OutcomeStatus | Literal["superseded"] | None`:
  - `None` stays "early copy deferred".
  - `SUPERSEDED: Final = "superseded"`.
  - `COMMIT_FAILED: Final = "commit_failed"`.
- `PgTransactions.__init__(self, database, *, idle_limit: timedelta | None = None)`. When it is
  set, `begin()` issues `SET LOCAL idle_in_transaction_session_timeout` first.
- `RUN_IDLE_LIMIT: Final = timedelta(seconds=2)` in `central/infra/runtime.py` (a placeholder, and
  lower than the 5s `lock_timeout` in `central/db.py:50`).

**Executor behaviour**
1. The opening read (`execution.py:109`, `_read`) returns the stored row AND draws the start number
   in the same transaction, before the early-copy check and before the handler runs.
2. `_record_ok` writes the outcome upsert FIRST, then `record_produced`. If the upsert returns
   `None`, the executor raises an internal marker inside the `with` block so the transaction rolls
   back, and returns `"superseded"`.
3. `_transient` and `terminal`: a refused record returns `"superseded"` and never calls `redeliver`.
4. The run transaction fails for any reason other than `ProducedFactsConflict` (for example a lock
   timeout, `IdleInTransactionSessionTimeout`, or a lost connection):
   - log it, then record `transient commit_failed` through `_transient`, which is conditional and
     redelivered by backoff;
   - if that write also raises, propagate the error, as today.
5. `ProducedFactsConflict` stays `terminal facts_conflict` (conditional).

**Runtime:** `JobRuntime._body` raises `RecordedFailure` only for `transient` and `terminal`.
`"superseded"` and `None` end the row `succeeded`.

**Wiring:** `build_job_runtime` (`central/content_wiring.py`) builds its `PgTransactions` with
`idle_limit=RUN_IDLE_LIMIT`. `build_content_services` (Central) passes none. Thread this through
`_core`.

**Update the module docstrings** of `execution.py`, `outcomes.py` and `runtime.py` to say that
results are ordered by start.

## Files

- **Code:** `central/migrations/028_outcome_start_number.sql` (new), `central/infra/outcomes.py`,
  `central/infra/execution.py`, `central/infra/runtime.py`, `central/infra/transactions.py`,
  `central/content_wiring.py`.
- **Tests:** `tests/test_run_order.py` (new), `tests/test_infra_execution.py`,
  `tests/test_infra_job_queue.py`, `tests/test_infra_outcome_feed.py`, `tests/test_infra_queue_ops.py`,
  `tests/test_publisher_conformance.py`, `tests/test_infra_runtime_boot.py`,
  `tests/test_content_wiring.py`.

## Existing tests it carries

- `tests/test_infra_execution.py:60-246`: the executor harness and every executor test.
- `tests/test_infra_job_queue.py:290-347`: callers of `record`, which now pass `start_number`.
- `tests/test_infra_outcome_feed.py:28-31`, `tests/test_infra_queue_ops.py:277` and
  `tests/test_publisher_conformance.py:143-151`: helpers that call `record`.
- `tests/test_infra_runtime_boot.py`: add the `_body` mapping for `"superseded"`.
- `tests/test_content_wiring.py:53-72`: the worker's transactions carry the idle limit, and
  Central's carry none.

## Acceptance criteria (`tests/test_run_order.py`, PostgreSQL)

The setup uses two `JobRuntime`s through the `pg_runtime` harness (`tests/test_infra_runtime_boot.py:421-429`).
Run A is rescued by backdating its worker's `last_heartbeat` by 1h and running `RescueStalledJobs`,
so that run B can start.

1. **T1:** a waiter publishes `FetchPackage`, and A's handler blocks. B stores `ok` with its facts.
   A is then released and returns `terminal`. Expect: the stored `ok` carries B's start number;
   exactly one NOTIFY for the key; the waiter gets `Ready`; and no redelivery row from A.
2. **T1a:** as T1, but A returns `transient` after B's `ok`. Expect: refused, and no redelivery row.
3. **T1b:** B stores `transient` first, then A returns `ok` with facts. Expect: A refused, and the
   asset's produced facts still empty right after A.
4. **T5:** A's `record_produced` sleeps 3s on its thread, holding the run transaction idle after the
   upsert. B's run transaction for the same key proceeds.
   Expect:
   - A's transaction ends in 25P03;
   - exactly one NOTIFY, which is B's;
   - A's `commit_failed` is refused.
5. **Migration:** 028 applies on the 027 schema, and `SELECT cache_size FROM pg_sequences WHERE
   sequencename='job_start_seq'` returns 1.
6. **Commit failure:** a lock timeout injected in the run transaction records `transient
   commit_failed` and calls `redeliver`.

## Mutation probes (each must turn the named test red; restore by reversing the edit)

- M1: remove the `WHERE` from the upsert (T1).
- M2: draw the start number at write time instead of in the opening read (T1).
- M3: send the NOTIFY when refused (T1: two NOTIFYs).
- M4: redeliver when refused (T1a).
- M5: write the facts before the upsert and do not roll back on refusal (T1b).
- M6: no idle limit on the worker's transactions (T5: A commits at 3s, so two NOTIFYs).
- M7: `CACHE 20` in 028 (the migration test).

## Report back

Report the net line delta, the reuse you considered, and any errata appended to `.claude/errata.md`
where this page is wrong.
