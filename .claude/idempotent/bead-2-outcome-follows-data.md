# Bead 2: `outcome-follows-data`

**Design:** `docs/central-idempotent-jobs.md`, rule 3, §6 (the whole section), §11 (defects 1, 2,
N1, N3). Read those sections only.
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1. It runs in parallel with beads 3 and 4, each in its own worktree. Its files
are disjoint from theirs. **Do not edit `tests/test_infra_job_queue.py`** (bead 4 owns it): if it
turns red, STOP and append an errata entry.
**Packages:** `central/infra`, `central/assets` (the reader), `central/kernel` (a docstring only),
plus `central/content_wiring.py`.

## Behaviour

1. `ok` is written with its data, as today.
2. A failure (`transient` or `terminal`) is written only while the job's data is absent. The
   failure's transaction locks the asset row, then checks for facts and the file. If the data is
   present, nothing is written: no NOTIFY and no redelivery, and the row ends `succeeded`. A job
   type with no asset has no data of its own, so its failures are always written.
3. A `transient` never replaces a standing `terminal`, and refused means no redelivery. A publish
   with `retry_terminal=True` withdraws the key's `terminal` before it enqueues.
4. After any wait, the reader tries to open the asset from its record before it reports a failure.

## Frozen page

**Outcomes** (`central/infra/outcomes.py`)
- `JobOutcomes.record(self, tx, job, *, status, reason, retry_not_before, now) -> OutcomeRow | None`:
  - the upsert gains
    `WHERE NOT (job_outcomes.status = 'terminal' AND EXCLUDED.status = 'transient')`;
  - `pg_notify` runs only when a row came back;
  - None means refused because a terminal stands.
- `JobOutcomes.withdraw_terminal(self, tx, lock_key: str) -> bool`: `DELETE FROM job_outcomes WHERE
  lock_key = %s AND status = 'terminal'`. True if one was deleted.

**Publisher** (`central/infra/publisher.py`, `central/kernel/publishing.py`)
- In `publish`, inside the existing PB5 savepoint: when `latest.status == "terminal"` and
  `retry_terminal`, read `since = latest.seq` first, then `withdraw_terminal`, then `defer`.
- Kernel docstring: add "PB3a: with `retry_terminal`, the key's terminal outcome is withdrawn
  before the defer."
- `RecordingPublisher` (`tests/fakes/publisher.py`) mirrors PB3a and the terminal-stands rule.

**Data presence** (`central/infra/asset_records.py`, `central/infra/stored_assets.py`,
`central/infra/execution.py`)
- `PgAssetRecords.lock_produced(self, tx, key: AssetKey) -> AssetReady | None`:
  `SELECT produced_size, produced_sha256 FROM assets WHERE kind=%s AND identity=%s FOR UPDATE`.
  None when there is no row or no facts. It is concrete (infra), not on the kernel protocol.
- `PgAssetRecords.record_produced` calls `lock_produced` FIRST, then compares or sets as today. So
  both write paths take the same row lock before the outcome row.
- `DataPresence` (a Protocol in `central/infra/execution.py`, next to `OutcomeWriter`):
  `present(self, tx: Transaction, job: Job[Any]) -> bool`. It means "lock this job's data; is it
  present?". False for a job type with no asset kind.
- `LockedAssetPresence` (`central/infra/stored_assets.py`):
  `__init__(self, *, records: PgAssetRecords, store: CacheStore)`.
  `present` = `facts is not None and store.present(key, facts)`, where
  `facts = records.lock_produced(tx, asset_key(job))`.

**Executor** (`central/infra/execution.py`)
- `JobExecutor.__init__` gains `presence: DataPresence`.
- `SETTLED: Final = "settled"`.
  `execute(...) -> OutcomeStatus | Literal["settled"] | None`. None stays "early copy deferred".
- Every failure write (the `terminal` path, `_transient`, and `facts_conflict`) runs in ONE
  transaction:
  1. `if presence.present(tx, job)`: write nothing and return `"settled"`;
  2. otherwise `row = outcomes.record(...)`; `row is None` returns `"settled"`;
  3. only a written `transient` calls `redeliver`, after the commit, as today.
- `_record_ok` is unchanged in order: `record_produced` (which now locks), then the `ok` upsert.
- Update the module docstring with the three outcome rules.

**Runtime and wiring**
- `JobRuntime.__init__` gains `presence: DataPresence` and passes it to the executor. `_body`
  raises `RecordedFailure` only for `transient` and `terminal`. `"settled"` and None end
  `succeeded`.
- `build_job_runtime` (`central/content_wiring.py`) passes
  `LockedAssetPresence(records=core.assets, store=core.store)`.

**Reader** (`central/assets/reader.py`, `_produce_and_open`)
- After `handle.wait`, for every outcome, first try
  `_open_in_thread(self._open_first, (job,))` (the data). Serve it if it opens.
- Otherwise map the outcome exactly as today: `Ready` becomes `absent_after_ready`; `Failed` and
  `Pending` are unchanged.

**No migration. No new error codes.**

## Files

- **Code:** `central/infra/outcomes.py`, `central/infra/publisher.py`,
  `central/kernel/publishing.py`, `central/infra/asset_records.py`,
  `central/infra/stored_assets.py`, `central/infra/execution.py`, `central/infra/runtime.py`,
  `central/content_wiring.py`, `central/assets/reader.py`.
- **Tests:** `tests/test_outcome_rules.py` (new), `tests/fakes/publisher.py`, plus the carried
  files below.

## Existing tests it carries

- `tests/test_infra_execution.py`: the executor harness gains a `DataPresence` fake (default
  absent). Existing expectations are unchanged when the data is absent.
- `tests/test_infra_runtime_boot.py` and `tests/test_infra_queue_ops.py`: the `JobRuntime`
  constructions pass `presence`. Add the `"settled"` mapping to `succeeded`.
- `tests/test_publisher_conformance.py`: add PB3a and the terminal-stands rule for both
  publishers.
- `tests/test_infra_outcome_feed.py:28-31`: the `record` helper's return may now be None.
- `tests/test_infra_asset_records.py`: `record_produced` still refuses different facts.
- `tests/test_assets_reader.py`: a `Failed` wait whose data is present now serves.
- `tests/test_content_wiring.py`: the worker's runtime carries `LockedAssetPresence`.

## Acceptance criteria (`tests/test_outcome_rules.py`, PostgreSQL)

1. **O1 (defect 1):** a copy stores `ok` and facts for a `FetchPackage` key, and its file is on disk.
   A second run of the same key then returns `TerminalFailure`. Expect `"settled"`: the outcome row
   is unchanged (`ok`, same `seq`), there is no NOTIFY, and there is no redelivery.
2. **O2:** as O1, but the late run is `transient`. Expect `"settled"` and no redelivery row.
3. **O3 (the lock, both orders):** transaction F runs the failure path with the data absent and
   holds the asset row. Transaction K runs `_record_ok` and must block until F commits; the final
   outcome is `ok`. In reverse, K holds the lock; F blocks, then finds the data and writes nothing.
4. **O4 (wipe, N1):** facts recorded, file removed. A failing run records its failure.
5. **O5 (terminal stands):** `terminal`, then a late `transient`: refused, and no redelivery. A
   request publish (`retry_terminal=True`) withdraws the terminal; a `transient` then lands.
6. **O6 (decision 3):** with a terminal standing, `Prefetch` publishes nothing for the key, and a
   request publish enqueues one run.
7. **O7 (reader):** a waiter whose outcome is `Failed` while the facts and the file are present
   gets the file.

## Mutation probes

- M1: skip the presence check (O1: the row turns `terminal`).
- M2: `lock_produced` without `FOR UPDATE` (O3: F does not block K, and the final outcome is
  `terminal`).
- M3: drop the terminal-stands `WHERE` (O5: the transient lands and redelivers).
- M4: no withdraw on `retry_terminal` (O5: the request's transient is refused).
- M5: NOTIFY when refused (O1: two NOTIFYs).
- M6: the reader maps the outcome before trying the data (O7).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong. **Report where the spec is wrong.**
