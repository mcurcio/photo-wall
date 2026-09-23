# Lane A — job runtime, publisher, outcome feed, queue ops (`central.infra`)

Design: §4 (the queue-ops rows), §10.1 ("Mapping", "Retry by re-publishing"), §10.2 and §10.3.
Kernel: `P0-kernel.md`. Only this package imports `procrastinate`. Read procrastinate 3.9.0 in
`.venv`; do not re-derive it. It already provides: `configure_task(..., lock=, queueing_lock=,
connection=, schedule_at=, priority=)`, `AlreadyEnqueued`, `app.periodic(cron=, periodic_id=)`,
`run_worker_async(queues=, concurrency=, install_signal_handlers=False)`,
`JobManager.get_stalled_jobs(seconds_since_heartbeat=)`, `finish_job_by_id_async`, and `delete_old_jobs`.

## Owns (create; no other lane touches these)
- `central/infra/job_queue.py`, `central/infra/outcomes.py`, `central/infra/execution.py`,
  `central/infra/runtime.py`, `central/infra/publisher.py`, `central/infra/outcome_feed.py`,
  `central/infra/queue_ops.py`
- `central/migrations/020_job_outcomes.sql`
- `tests/test_infra_job_queue.py`, `tests/test_infra_execution.py`, `tests/test_infra_runtime_boot.py`,
  `tests/test_infra_publisher_conformance.py`, `tests/test_infra_outcome_feed.py`,
  `tests/test_infra_queue_ops.py`, and `tests/runtime_fakes.py` (lane-private fakes)

## Consumes
The kernel: `Job`, `job_keys`, `asset_key`, `registered_job_type`, `CATALOG`, `QueueName`, `Delivery`,
`Handler`, `handler_job_type`, the failures, `Ready`, `Failed`, `Pending`, `SettledHandle`, `NOT_PUBLISHED`,
`Publisher`, `Transaction`, `AssetRecords`, and `ProducedFactsConflict`. It also consumes `central.infra.transactions.PgTransactions`
and `pg_connection`, plus `central.db.Database`. Tests use `fakes.*`.

## Must not touch
Anything outside the list above. That includes `pyproject.toml`, `tests/fakes/`, other `central/infra/*` files,
`central/app.py`, `media/`, and legacy `central/*.py`.

## Migration `020_job_outcomes.sql`
```sql
CREATE SEQUENCE job_outcome_seq;
CREATE TABLE job_outcomes (
    lock_key TEXT PRIMARY KEY CHECK (length(lock_key) BETWEEN 1 AND 1024),
    job_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','transient','terminal')),
    reason TEXT CHECK (reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    retry_not_before DOUBLE PRECISION,
    failing_since DOUBLE PRECISION,
    seq BIGINT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    CHECK ((status = 'ok') = (reason IS NULL)),
    CHECK ((status = 'ok') = (failing_since IS NULL)),
    CHECK (status = 'transient' OR retry_not_before IS NULL)
);
CREATE INDEX job_outcomes_updated ON job_outcomes (updated_at);
```

## Provides (frozen signatures)

```python
# job_queue.py — the ONLY mapping between kernel jobs and procrastinate
TASK_PREFIX: Final = "photo_wall."
ATTEMPT_KWARG: Final = "_attempt"   # procrastinate passes kwargs verbatim (worker.py:301); pydantic forbids "_" fields, so no collision
def task_name(job_type: type[Job[Any]]) -> str: ...                 # TASK_PREFIX + job_name
def job_kwargs(job: Job[Any], *, attempt: int) -> dict[str, Any]: ...  # model_dump(mode="json") + attempt
def decode(task_name: str, kwargs: Mapping[str, Any]) -> tuple[Job[Any], int]: ...
    # drops "timestamp" (periodic), pops the attempt (default 0); KeyError for an unknown task
def periodic_cron(every: timedelta) -> str: ...   # seconds-last six-field croniter; ValueError outside PERIODIC_CADENCES
TaskBody = Callable[[Job[Any], int], Awaitable[None]]
def build_app(connector: procrastinate.BaseConnector, job_types: Sequence[type[Job[Any]]],
              body: TaskBody | None) -> procrastinate.App: ...
    # one task per type: name=task_name, queue=delivery.queue.value, priority, retry=None,
    # pass_context=True; periodic types additionally app.periodic(cron=periodic_cron(every),
    # periodic_id=job_name) with the constant lock/queueing_lock of the field-less job.
    # body None → publish-only app (the task raises if ever executed)
def defer(app: procrastinate.App, job: Job[Any], *, attempt: int, connection: Any,
          schedule_at: datetime | None = None) -> bool: ...            # True inserted, False merged
async def defer_async(app: procrastinate.App, job: Job[Any], *, attempt: int,
                      schedule_at: datetime | None = None) -> bool: ...

# outcomes.py
OutcomeStatus: TypeAlias = Literal["ok", "transient", "terminal"]
@dataclass(frozen=True, slots=True)
class OutcomeRow:
    lock_key: str; job_name: str; status: OutcomeStatus; reason: str | None
    retry_not_before: float | None; failing_since: float | None; seq: int; updated_at: float
class JobOutcomes:
    def get(self, tx: Transaction, lock_key: str) -> OutcomeRow | None: ...
    def get_many(self, tx: Transaction, lock_keys: Collection[str]) -> dict[str, OutcomeRow]: ...
    def record(self, tx: Transaction, job: Job[Any], *, status: OutcomeStatus, reason: str | None,
               retry_not_before: float | None, now: float) -> OutcomeRow: ...
        # upsert; seq = nextval('job_outcome_seq'); failing_since kept while failing, cleared on ok;
        # SELECT pg_notify('job_outcome', lock_key) in the same transaction
    def purge(self, tx: Transaction, *, older_than: float) -> int: ...

# execution.py — the per-job semantics, free of procrastinate so it is unit-testable
@dataclass(frozen=True, slots=True)
class Redelivery:
    job: Job[Any]; attempt: int; not_before: float
class OutcomeWriter(Protocol):                                       # JobOutcomes satisfies it
    def get(self, tx: Transaction, lock_key: str) -> OutcomeRow | None: ...
    def record(self, tx: Transaction, job: Job[Any], *, status: OutcomeStatus, reason: str | None,
               retry_not_before: float | None, now: float) -> OutcomeRow: ...
MAX_RETRY_DELAY: Final = timedelta(hours=1)
class JobExecutor:
    def __init__(self, handlers: Sequence[Handler[Any, Any]], *, transactions: Transactions,
                 outcomes: OutcomeWriter, assets: AssetRecords, clock: Clock,
                 redeliver: Callable[[Redelivery], Awaitable[None]],
                 catalog: Sequence[type[Job[Any]]] = CATALOG) -> None: ...
        # BOOT CHECKS (TypeError/ValueError, i.e. fail at worker start): handler_job_type per handler;
        # two handlers for one type; a CATALOG type with no handler; a handler for a non-CATALOG type
    @property
    def queues(self) -> frozenset[QueueName]: ...
    async def execute(self, job: Job[Any], attempt: int) -> OutcomeStatus | None: ...
        # None = deferred without running (early copy inside a retry window)

# runtime.py
class JobRuntime:                                  # the one worker kind
    def __init__(self, dsn: str, handlers: Sequence[Handler[Any, Any]],
                 concurrency: Mapping[QueueName, int], *, transactions: PgTransactions,
                 assets: AssetRecords, clock: Clock,
                 catalog: Sequence[type[Job[Any]]] = CATALOG) -> None: ...
        # builds JobExecutor (all boot checks) + build_app(PsycopgConnector(dsn), catalog, body);
        # ValueError unless concurrency has exactly the executor's queues, each >= 1
    async def run(self) -> None: ...               # one Worker per queue on one App, in a TaskGroup
    def stop(self) -> None: ...                    # graceful stop of every loop; idempotent

# outcome_feed.py
class OutcomeFeed:                                  # one per process; one LISTEN connection
    def __init__(self, dsn: str, *, transactions: Transactions, outcomes: JobOutcomes,
                 recheck: timedelta = timedelta(seconds=1)) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def wait_for(self, lock_key: str, *, since: int, timeout: timedelta) -> OutcomeRow | None: ...
        # returns at once if the stored row already has seq > since; otherwise registers (waiters for
        # one key share one entry), resolves on NOTIFY or the recheck read, None on timeout; cancel-safe

# publisher.py
class ProcrastinatePublisher:                       # implements kernel Publisher (PB1–PB9)
    def __init__(self, dsn: str, *, transactions: PgTransactions, outcomes: JobOutcomes,
                 assets: AssetRecords, clock: Clock, feed: OutcomeFeed | None,
                 job_types: Sequence[type[Job[Any]]] = CATALOG) -> None: ...
        # feed None (the worker) → a handle's wait raises RuntimeError("no_outcome_feed")
    def publish(self, job: Job[R], *, within: Transaction, retry_terminal: bool = False) -> JobHandle[R]: ...
    async def publish_now(self, job: Job[R], *, retry_terminal: bool = False) -> JobHandle[R]: ...

# queue_ops.py
@dataclass(frozen=True, slots=True)
class StalledJob:
    id: int; job: Job[Any]; attempt: int
class QueueAdmin:
    def __init__(self, dsn: str, *, job_types: Sequence[type[Job[Any]]] = CATALOG) -> None: ...
    async def stalled(self, *, heartbeat_timeout: timedelta) -> list[StalledJob]: ...  # ours only (TASK_PREFIX + registered)
    async def republish(self, stalled: StalledJob) -> None: ...                         # defer_async, same attempt
    async def close(self, stalled: StalledJob) -> None: ...                             # finish as failed → frees the lock
    async def delete_finished(self, *, older_than: timedelta) -> None: ...             # delete_old_jobs incl. failed/cancelled/aborted
class RescueStalledJobsHandler:
    def __init__(self, admin: QueueAdmin, *, heartbeat_timeout: timedelta = timedelta(seconds=30)) -> None: ...
    async def handle(self, job: RescueStalledJobs) -> None: ...      # per stalled: republish THEN close
class PurgeFinishedJobsHandler:
    def __init__(self, admin: QueueAdmin, *, transactions: Transactions, outcomes: JobOutcomes,
                 clock: Clock, keep_jobs: timedelta = timedelta(days=7),
                 keep_outcomes: timedelta = timedelta(days=30)) -> None: ...
    async def handle(self, job: PurgeFinishedJobs) -> None: ...
```

## Behaviour (the acceptance criteria; each has a test)

**A1 Execution (`JobExecutor.execute`, unit-tested with fakes and a recording `redeliver`)**
1. *Early copy.* If the stored outcome is `transient` and `now < retry_not_before - 1s`, the job is re-delivered
   at `retry_not_before` with the same attempt. The handler does not run, no outcome is written, and it returns `None`.
2. *Returned `R`.* In ONE transaction:
   - an asset job gets `assets.record_produced(asset_key(job), R)`
   - then `outcomes.record(ok)` with its NOTIFY

   It returns `"ok"`. A `ProducedFactsConflict` is logged as a bug and becomes the outcome `terminal("facts_conflict")`.
3. *`TerminalFailure(reason)`:* the outcome is `terminal(reason)`. There is no redelivery.
4. *`TransientFailure` or any other `Exception`.* An unclassified exception is logged with its traceback as a handler bug, with
   reason `"unclassified_error"`. With `n = attempt`:
   - If `n < len(delivery.retry)`: `delay = min(max(retry[n], retry_after or 0), MAX_RETRY_DELAY)`.
     Record `transient` with `retry_not_before = now + delay`, then redeliver `attempt=n+1` at that time.
   - Otherwise: record `transient` with `retry_not_before = now + min(retry_after or 0, MAX_RETRY_DELAY)`,
     and do not redeliver.
5. *Ordering and propagation.* The outcome is committed BEFORE the redelivery is requested. `CancelledError` propagates
   untouched: no outcome, no redelivery.
   In `runtime.py`, the procrastinate task body raises `RecordedFailure(reason)` after a `transient` or `terminal`
   outcome. With `retry=None`, procrastinate ends that row `failed` and never retries it in place. `ok` and `None`
   return normally, and the row ends `succeeded`.
6. *Boot checks.* Each rule in the `JobExecutor.__init__` comment has its own failing test.

**A2 Mapping (`job_queue`)**
- `decode(task_name(T), job_kwargs(j, attempt=k)) == (j, k)` for every CATALOG type and for a
  hypothesis-generated test type.
- `periodic_cron` covers every member of `PERIODIC_CADENCES`. Assert the expressions: 5 min → `"*/5 * * * * 0"`,
  1 h → `"0 */1 * * * 0"` (seconds-last, the existing convention in `media/task_queue.py`).
- `build_app` registers exactly one task per type, with `retry=None` and the queue and priority from `Delivery`.
- `defer` passes both locks from `job_keys` and reports `AlreadyEnqueued` as `False`.

**A3 Runtime boot**
`JobRuntime(...)` with a missing handler, a duplicate handler, or a concurrency map that does not match
fails in `__init__` (no DB needed). `run()` starts one `run_worker_async(queues=[q])` per queue with
`install_signal_handlers=False`, heartbeat 10s, and stalled timeout 30s.

**A4 Publisher conformance.** `tests/test_infra_publisher_conformance.py` parametrizes PB1–PB9 over
`RecordingPublisher` (it runs locally) and `ProcrastinatePublisher` (it needs `PHOTO_WALL_TEST_DATABASE_URL`;
skip otherwise, as `tests/conftest.py` does). Cases:
- ENQUEUED, merged-into-pending, and joined-while-running all return equivalent handles.
- `since` ignores an older `ok`.
- A run that finishes between the since-read and the defer resolves the handle.
- Rollback → `NOT_PUBLISHED`.
- Waiting inside the open transaction → `RuntimeError`.
- Timeout → `Pending`.
- Cancel → unregistered; the job still completes.
- A retry window suppresses publishing, and so does a terminal outcome unless `retry_terminal` is set.
- A periodic type merges into its pending tick.
- A merge inside `within` leaves the caller's other writes committed (PB5).

**Implementation notes**
- `publish_now` = `asyncio.to_thread` over `transactions.begin()` + `publish`. It is a short DB write, not a
  wait. This is why there is no async pool. The design said "a small async psycopg pool". Report it as a deviation.
- `publish` uses a publish-only `build_app(SyncPsycopgConnector(dsn), job_types, None)` and
  `defer(..., connection=pg_connection(within))` inside `conn.transaction()` (a savepoint, the
  `media_queue.py:51` pattern).

**A5 Outcome feed (DB tests; CI)**
- One process with 50 waiters on one key uses one LISTEN connection and one entry.
- A NOTIFY that fires before the waiter registered is still seen through the stored row.
- After the LISTEN connection drops, the 1s recheck still resolves the waiter.

**A6 Queue ops (DB tests; CI)**
- A job whose worker's heartbeat is > 30s old is re-published THEN closed. The new copy runs only after the close
  frees the lock (`procrastinate_fetch_job_v2` skips a todo whose lock a doing row holds).
- Legacy `photo_wall.media.*` jobs are never touched.
- Purge deletes finished rows older than `keep_jobs` and outcome rows not written for `keep_outcomes`.

**A7 Worker-side retry effect (DB; CI).** A job that raises `TransientFailure` twice and then succeeds:
- leaves exactly one `todo` row at a time
- ends with outcome `ok`
- never uses procrastinate's in-place `retry_job`

This is the design's "CI must test the worker-side effect".

## Beads (serial inside the lane)
| Bead | Scope | Gate (local) |
| --- | --- | --- |
| A-1 | `job_queue.py`, `outcomes.py`, migration 020, `test_infra_job_queue.py` | unit + ruff + lint-imports |
| A-2 | `execution.py`, `runtime.py`, `test_infra_execution.py`, `test_infra_runtime_boot.py` | same |
| A-3 | `publisher.py`, `outcome_feed.py`, conformance + feed tests | same (the DB cases skip locally) |
| A-4 | `queue_ops.py`, `test_infra_queue_ops.py` | same |
