# P0 — kernel, package skeleton, transactions, fakes (FROZEN page)

Design: `docs/central-system-architecture.md` §10 (read §10.1–§10.5 only).
This phase is serial, and nothing else starts until P0.2 lands. An implementer who hits a wall appends to
`.claude/errata.md` and STOPS. Never edit this page locally.

There are two beads, and each must be green on its own: `pytest -q tests/test_kernel_*.py tests/test_fakes_*.py
tests/test_infra_transactions.py`, `ruff check .`, and `lint-imports`. If `uv run` touched `uv.lock`, revert it.
Add no new dependencies.

| Bead | Files (create) | Also |
| --- | --- | --- |
| P0.1 | `central/kernel/{__init__,types,assets,jobs,job_types,publishing,handling,transactions,ports}.py`; empty package markers `central/{infra,origins,content_catalog,assets,health}/__init__.py` (one-line docstring each); `tests/test_kernel_{types,jobs,publishing,handling,ports}.py` | `pyproject.toml`: the import-linter contracts below (the ONLY P0 edit to pyproject) |
| P0.2 | `central/infra/transactions.py`; `tests/fakes/{__init__,transactions,publisher,asset_records,origin,catalog}.py`; `tests/test_infra_transactions.py`, `tests/test_fakes_publisher.py` | — |

These conventions bind every file:
- Every file starts with `from __future__ import annotations`.
- The kernel imports only stdlib, `pydantic`, `semver` and itself. The design says "pydantic + stdlib". `semver` is added because `ReleaseTag` must reuse the existing strict parse (`central/app_releases.py:44`).
- Timestamps are epoch-second `float`s from `contracts.time.Clock`. That is the repo's DB convention (`DOUBLE PRECISION` columns).
- Durations are `timedelta`s.
- `central/kernel/__init__.py` holds only a docstring. Importers name the submodule.

---

## `central/kernel/types.py`

```python
class ReleaseVersion(NamedTuple):
    major: int
    minor: int
    patch: int
    prerelease: str                       # "" for a full release
    def order_key(self) -> tuple[int, int, int, bool, str]: ...
        # (major, minor, patch, prerelease == "", prerelease) — the existing _rank (netboot_base.py:350)

def release_version(tag: str) -> ReleaseVersion: ...
    # "v" + strict SemVer 2.0.0 via semver.Version.parse(tag[1:]); build metadata ("+x") REJECTED;
    # len(tag) <= 128. Raises ValueError("invalid_tag"). Port of app_releases.parse_semver.
def require_sha256(value: str) -> str: ...        # ^[0-9a-f]{64}$ else ValueError("invalid_sha256")
def require_reason(value: str) -> str: ...        # ^[a-z][a-z0-9_]{0,63}$ else ValueError("invalid_reason")

ReleaseTag = Annotated[str, AfterValidator(_release_tag)]   # validates via release_version, returns the tag
Sha256 = Annotated[str, AfterValidator(require_sha256)]
ReasonCode = Annotated[str, AfterValidator(require_reason)]
```

## `central/kernel/assets.py`

```python
class AssetKind(StrEnum):
    OS_IMAGE = "os-image"
    PLAYER_DEB = "player-deb"             # media-variant arrives with media (co-change bead)

@dataclass(frozen=True, slots=True)
class AssetKey:
    kind: AssetKind
    identity: str                          # 1..256 chars; no "/", "\\", "\0"; not "." / ".."

@dataclass(frozen=True, slots=True)
class AssetReady:                          # the R of every asset job = the write-once "produced facts"
    size: int                              # > 0
    sha256: str                            # require_sha256

@dataclass(frozen=True, slots=True)
class OriginLocator:                       # where to get the DOWNLOAD and how to verify it
    url: str                               # "https://" or "http://", len <= 2048
    sha256: str | None                     # digest of the downloaded bytes, when the origin states it
    size: int | None                       # > 0 when set

@dataclass(frozen=True, slots=True)
class AssetReference:
    owner: str                             # the release tag that ships it (MVP); 1..128 chars
    locator: OriginLocator
    expected_size: int | None              # facts of the PRODUCED file, when known up front:
    expected_sha256: str | None            #   .deb → == locator's; os-image → None (the tarball
                                           #   digest is not the squashfs digest; doc §10.4 fixed)

@dataclass(frozen=True, slots=True)
class Asset:
    key: AssetKey
    references: tuple[AssetReference, ...] # NON-EMPTY, owners unique, NEWEST FIRST (by time added)
    produced: AssetReady | None
    last_served_at: float | None
```
Every `__post_init__` raises `ValueError` when an invariant is violated, so a bad value fails at construction.

## `central/kernel/jobs.py`

```python
class QueueName(StrEnum):
    FETCH = "photo-wall-fetch"
    UPKEEP = "photo-wall-upkeep"           # TRANSCODE arrives with media

PERIODIC_CADENCES: Final[frozenset[timedelta]]
    # minutes {1,2,3,4,5,6,10,12,15,20,30} ∪ hours {1,2,3,4,6,8,12,24}: exactly the cadences a
    # seconds-last croniter expression can say (infra/job_queue.periodic_cron maps them).

@dataclass(frozen=True, slots=True)
class Delivery:
    queue: QueueName
    retry: tuple[timedelta, ...] = ()      # backoff delays; () = no retry; each 0 < d <= 1 day
    priority: int = 0                      # -100..100
    every: timedelta | None = None         # periodic; must be in PERIODIC_CADENCES
    # __post_init__ → ValueError on any violation

R = TypeVar("R")

class Job(BaseModel, Generic[R]):
    model_config = ConfigDict(frozen=True, extra="forbid")
    job_name: ClassVar[str]
    delivery: ClassVar[Delivery]
    subject: ClassVar[tuple[str, ...]]
    asset_kind: ClassVar[AssetKind | None]
    result_type: ClassVar[type[Any] | None]   # None ⇔ Job[None]

    def __init_subclass__(cls, **kwargs: Any) -> None: ...
        # swallows the class keywords (name/delivery/subject/asset) so object.__init_subclass__
        # does not reject them; pydantic passes them again to the hook below (probed, pydantic 2.11.4)
    @classmethod
    def __pydantic_init_subclass__(
        cls, *, name: str | None = None, delivery: Delivery | None = None,
        subject: tuple[str, ...] | None = None, asset: AssetKind | None = None, **kwargs: Any,
    ) -> None: ...

@dataclass(frozen=True, slots=True)
class JobKeys:
    lock: str                              # one RUNNING copy fleet-wide; also the job_outcomes key + NOTIFY payload
    queueing_lock: str                     # one PENDING copy

def job_keys(job: Job[Any]) -> JobKeys: ...
def asset_key(job: Job[Any]) -> AssetKey: ...            # TypeError if type(job).asset_kind is None
def registered_job_type(name: str) -> type[Job[Any]]: ... # KeyError if unknown
```

**Class-definition checks.** `__pydantic_init_subclass__` runs these checks, and every failure raises `TypeError`.
The class is registered only after ALL of them pass. Parametrized intermediates such as
`Job[AssetReady]` return at once, because they are not job types. Detect them with
`cls.__pydantic_generic_metadata__["origin"] is not None`. `R` comes from the single base's
`__pydantic_generic_metadata__["args"]`.

1. The base is not a parametrized `Job[...]`. That covers a bare `Job` and subclassing a concrete job type.
2. `name` is missing, does not match `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`, is longer than 64, or is
   already registered.
3. `delivery` is missing or is not a `Delivery`.
4. The class body defines any of `job_name, delivery, subject, asset_kind, result_type,
   model_config, __init_subclass__, __pydantic_init_subclass__`, or a field named `timestamp`
   (the procrastinate mapping reserves it).
5. A field is not keyable. After pydantic strips `Annotated`, its annotation must be `str`, `int`, `bool`, or an `Enum`
   subclass whose values are all `str` or all `int`. Unions, `None`, floats and containers are refused.
6. `subject` names an unknown field or repeats one. `subject=None` means all fields, in declaration order.
7. `delivery.every` is set on a job that has fields.
8. The result type and asset kind disagree:
   - `R` is not `None` but `asset` is missing.
   - `asset` is set but `R` is not `AssetReady` or a subclass.
   - `asset` is set with `len(subject) != 1` (multi-field asset keys arrive with media).
   - `asset` is set together with `every`.
   - `asset` is already claimed by another job type.

**Key derivation.** Callers never write or override the keys.
Let `p = job.model_dump(mode="json")` and
`enc(xs) = json.dumps(xs, separators=(",", ":"), ensure_ascii=False)`. Then:
- `lock = job_name + enc([p[f] for f in subject])`
- `queueing_lock = job_name + enc([p[f] for f in model_fields])`

A job name cannot contain `[`, so both keys are injective.
`asset_key(job) = AssetKey(asset_kind, str(p[subject[0]]))`, so an asset's lock and its disk key name the same thing.
`job_keys` and `asset_key` raise `TypeError` for an instance whose type is not registered.

## `central/kernel/job_types.py` (the MVP job catalog)

```python
_FETCH_RETRY = (timedelta(seconds=5), timedelta(minutes=1), timedelta(minutes=5))

class FetchOsImage(Job[AssetReady], name="os_image.fetch", asset=AssetKind.OS_IMAGE,
                   delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    tag: ReleaseTag
class FetchPackage(Job[AssetReady], name="player_deb.fetch", asset=AssetKind.PLAYER_DEB,
                   delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    sha256: Sha256
class SyncReleases(Job[None], name="releases.sync",
                   delivery=Delivery(queue=QueueName.FETCH, every=timedelta(minutes=15))): ...
class Prefetch(Job[None], name="assets.prefetch",
               delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=5))): ...
class RescueStalledJobs(Job[None], name="queue.rescue_stalled",
                        delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=1))): ...
class PurgeFinishedJobs(Job[None], name="queue.purge_finished",
                        delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(hours=1))): ...

AssetJob: TypeAlias = FetchOsImage | FetchPackage
CATALOG: Final[tuple[type[Job[Any]], ...]] = (
    FetchOsImage, FetchPackage, SyncReleases, Prefetch, RescueStalledJobs, PurgeFinishedJobs)
```
`JobRuntime` requires exactly one handler for each type in `CATALOG`. Define test-only job types at test-module
scope with a `test.` name prefix. They never enter `CATALOG`.

## `central/kernel/publishing.py`

```python
R_co = TypeVar("R_co", covariant=True)

@dataclass(frozen=True, slots=True)
class Ready(Generic[R]):
    result: R                              # asset job: AssetReady read from the Asset record; else None
@dataclass(frozen=True, slots=True)
class Failed:
    terminal: bool
    reason: str                            # require_reason
    retry_after: timedelta | None          # terminal ⇒ None; else >= 0  (ValueError otherwise)
@dataclass(frozen=True, slots=True)
class Pending: ...

NOT_PUBLISHED: Final[Failed] = Failed(terminal=True, reason="not_published", retry_after=None)

class JobHandle(Protocol[R_co]):
    async def wait(self, *, timeout: timedelta) -> Ready[R_co] | Failed | Pending: ...

class SettledHandle(Generic[R]):           # shared by every Publisher for outcomes decided at publish time
    def __init__(self, outcome: Ready[R] | Failed) -> None: ...
    async def wait(self, *, timeout: timedelta) -> Ready[R] | Failed | Pending: ...  # returns the outcome at once

class Publisher(Protocol):
    def publish(self, job: Job[R], *, within: Transaction,
                retry_terminal: bool = False) -> JobHandle[R]: ...
    async def publish_now(self, job: Job[R], *, retry_terminal: bool = False) -> JobHandle[R]: ...
```

**Publisher contract.** `RecordingPublisher` and the procrastinate adapter must both pass one
conformance suite, which Lane A writes.
- **PB1** An unregistered job type raises `TypeError`. `within.state != "open"` raises `RuntimeError("transaction_not_open")`.
- **PB2** If the latest outcome for `job_keys(job).lock` is `transient` with `retry_not_before > now`,
  `publish` inserts nothing and returns `SettledHandle(Failed(False, reason, retry_not_before - now))`.
- **PB3** If the latest outcome is `terminal` and `retry_terminal` is not set, `publish` inserts nothing
  and returns `SettledHandle(Failed(True, reason, None))`.
- **PB4** Otherwise `publish` reads `since` (the key's current outcome sequence, 0 if none), then defers.
  Merging into a pending copy counts as success. A running copy is joined, not duplicated.
- **PB5** `publish` runs in a SAVEPOINT of `within`, so a merge never aborts the caller's work.
- **PB6** `wait` while `within` is still open raises `RuntimeError("await_after_commit")`. After a
  rollback, `wait` returns `NOT_PUBLISHED`.
- **PB7** `wait` resolves on the first outcome whose sequence is greater than `since`:
  - `ok` → `Ready`
  - `transient` → `Failed(False, reason, max(0, retry_not_before - now))`
  - `terminal` → `Failed(True, reason, None)`
  - timeout → `Pending`
  - cancellation → unregister and re-raise

  Neither a timeout nor a cancellation cancels the job.
- **PB8** `publish_now` uses its own transaction and commits it before returning.
- **PB9** Periodic job types may be published on demand. They merge into the pending tick.

## `central/kernel/handling.py`

```python
J_contra = TypeVar("J_contra", bound=Job[Any], contravariant=True)

class Handler(Protocol[J_contra, R_co]):
    async def handle(self, job: J_contra) -> R_co: ...

class TransientFailure(Exception):
    def __init__(self, reason: str, *, retry_after: timedelta | None = None) -> None: ...
    reason: str                            # require_reason
    retry_after: timedelta | None          # >= 0
class TerminalFailure(Exception):
    def __init__(self, reason: str) -> None: ...
    reason: str
class OriginUnavailable(TransientFailure): ...  # network, 5xx, 403/429 rate limit (+retry_after), corrupt/truncated stream
class OriginRejected(TerminalFailure): ...      # 404/410, oversize, schema/manifest errors
class ProducedFactsConflict(Exception): ...     # record_produced given facts ≠ the recorded facts

def handler_job_type(handler: object) -> type[Job[Any]]: ...
```
`handler_job_type` reads `typing.get_type_hints(type(handler).handle)`. The `job` parameter
must be a registered job type, and the `return` hint must equal that type's `result_type` (`None` ↔
`NoneType`). Anything else raises `TypeError`. Handler modules must therefore import job types at runtime,
never under `TYPE_CHECKING`. Say so in each handler module.

## `central/kernel/transactions.py`

```python
TxState: TypeAlias = Literal["open", "committed", "rolled_back"]

class Transaction(Protocol):              # opaque to domains; infra unwraps its own implementation
    @property
    def state(self) -> TxState: ...

class Transactions(Protocol):
    def begin(self) -> AbstractContextManager[Transaction]: ...
```
Contract:
- `begin()` yields an `open` transaction.
- A normal exit commits (`committed`).
- An exception rolls back (`rolled_back`) and re-raises.
- It blocks, so call it from a worker thread, never on the event loop.
- It does not nest.

**Deliberately absent: `AUTOCOMMIT`.** Every sync publisher in the MVP already has an enclosing
transaction, and async callers use `publish_now`. A sentinel would be a second way to say the same thing.

## `central/kernel/ports.py`

```python
@dataclass(frozen=True, slots=True)
class NetbootBaseRequest:
    serial: str | None                     # raw header; the catalog sanitizes it
@dataclass(frozen=True, slots=True)
class PackageRequest:
    sha256: str                            # require_sha256 → ValueError
ContentRequest: TypeAlias = NetbootBaseRequest | PackageRequest

@dataclass(frozen=True, slots=True)
class Candidates:
    jobs: tuple[AssetJob, ...]             # non-empty, no duplicates, one asset kind; preference order
    pinned: bool                           # pinned ⇒ len(jobs) == 1   (ValueError otherwise)
@dataclass(frozen=True, slots=True)
class Unknown:
    reason: str                            # require_reason
Resolution: TypeAlias = Candidates | Unknown

class ContentCatalog(Protocol):
    async def resolve(self, request: ContentRequest) -> Resolution: ...
    async def desired_assets(self) -> frozenset[AssetJob]: ...

class AssetRecords(Protocol):
    def get(self, tx: Transaction, key: AssetKey) -> Asset | None: ...
    def reference(self, tx: Transaction, key: AssetKey, ref: AssetReference) -> bool: ...
        # upsert by (key, ref.owner); creates the asset row if absent; True iff new or changed
    def retire(self, tx: Transaction, key: AssetKey, owner: str) -> None: ...
        # deletes that reference; deletes the asset row with its last reference; absent → no-op
    def record_produced(self, tx: Transaction, key: AssetKey, facts: AssetReady) -> None: ...
        # write-once: equal facts → no-op; different → ProducedFactsConflict; row absent → no-op
    def touch_served(self, tx: Transaction, key: AssetKey, at: float) -> None: ...  # absent → no-op

@dataclass(frozen=True, slots=True)
class PublishedRelease:
    tag: str                               # release_version-valid
    is_prerelease: bool
    package: OriginLocator | None          # the Player .deb; url, sha256 and size all set when present
    package_problem: str | None            # require_reason; set iff package is None
    os_image: OriginLocator | None         # the base tarball; url, sha256 and size set when present
@dataclass(frozen=True, slots=True)
class ReleaseListing:
    releases: tuple[PublishedRelease, ...]
    etag: str | None
    unchanged: bool                        # 304: releases == ()

class ReleaseOrigin(Protocol):
    async def list_releases(self, *, etag: str | None) -> ReleaseListing: ...
    async def download(self, locator: OriginLocator, into: Path, *, max_bytes: int) -> None: ...
```
`download` contract:
- `into` must not exist. It is created `O_EXCL` with mode 0600.
- It streams at most `max_bytes`.
- It verifies `locator.size` and `locator.sha256` when they are set, then fsyncs.
- On ANY failure it removes `into` and raises `OriginUnavailable` or `OriginRejected`. A local `OSError` is re-raised
  as-is after the removal.

`list_releases` raises the same two errors on failure and never returns a partial list.

---

## `central/infra/transactions.py` (P0.2; the one infra file that three lanes share)

```python
class PgTransaction:                       # implements Transaction
    @property
    def state(self) -> TxState: ...
    @property
    def connection(self) -> psycopg.Connection[dict[str, Any]]: ...   # RuntimeError unless open

class PgTransactions:                      # implements Transactions
    def __init__(self, database: Database) -> None: ...   # central.db.Database (sync pool, SET LOCAL timeouts)
    @contextmanager
    def begin(self) -> Iterator[PgTransaction]: ...       # wraps database.transaction(); sets state on exit

def pg_connection(tx: Transaction) -> psycopg.Connection[dict[str, Any]]: ...
    # TypeError if tx is not a PgTransaction (a fake handed to a real repo); RuntimeError if not open
```

## `tests/fakes/` (P0.2; lanes import these, never edit them. Lane-private fakes go in `tests/<lane>_fakes.py`)

Import them as `from fakes.publisher import RecordingPublisher`. `tests/` is on `sys.path`, the same way
`tests/media_queue.py` already is. Async tests use `asyncio.run(...)`, the repo convention; there is no plugin.

```python
# transactions.py
class FakeTransaction: state: TxState                    # implements Transaction
class FakeTransactions:                                  # implements Transactions
    begun: list[FakeTransaction]
    def begin(self) -> AbstractContextManager[FakeTransaction]: ...
# publisher.py
@dataclass(frozen=True)
class PublishedCall:
    job: Job[Any]
    retry_terminal: bool
    within: Transaction | None
class RecordingPublisher:                                # implements Publisher, PB1–PB9 in memory
    def __init__(self, clock: Clock, assets: AssetRecords | None = None) -> None: ...
    calls: list[PublishedCall]                           # every call, including suppressed ones
    inserted: list[Job[Any]]                             # what a real queue would hold (PB2/PB3 skip)
    def record_outcome(self, job: Job[Any], outcome: Ready[Any] | Failed,
                       *, retry_not_before: float | None = None) -> None: ...  # simulates the runtime; wakes waiters
# asset_records.py
class InMemoryAssetRecords: ...                          # implements AssetRecords; asserts tx.state == "open"
# origin.py
class FakeReleaseOrigin:                                 # implements ReleaseOrigin
    def __init__(self, listing: ReleaseListing | Exception,
                 blobs: Mapping[str, bytes | Exception]) -> None: ...   # url → bytes, or raise
    downloads: list[OriginLocator]
# catalog.py
class StaticContentCatalog:                              # implements ContentCatalog
    def __init__(self, resolutions: Mapping[ContentRequest, Resolution],
                 desired: frozenset[AssetJob] = frozenset()) -> None: ...  # unmapped → Unknown("unknown")
```

## import-linter contracts (P0.1 adds all of them; later beads never edit them)

```toml
[[tool.importlinter.contracts]]
name = "Central content-serving layers"
type = "layers"
layers = ["central.app", "central.infra",
          "central.content_catalog | central.assets | central.health",
          "central.origins", "central.kernel"]

[[tool.importlinter.contracts]]
name = "Kernel, origins and domains have no persistence, queue or HTTP-framework dependency"
type = "forbidden"
source_modules = ["central.kernel", "central.origins", "central.content_catalog",
                  "central.assets", "central.health"]
forbidden_modules = ["procrastinate", "psycopg", "psycopg_pool", "fastapi", "starlette"]

[[tool.importlinter.contracts]]
name = "Kernel is vocabulary only"
type = "forbidden"
source_modules = ["central.kernel"]
forbidden_modules = ["httpx", "media", "central.db", "central.cache_layout", "central.artifact_io"]
```
Indirect imports are checked too, so a domain that reaches `central.db` fails through `psycopg`.

## Tests that prove P0 (behaviours; one test may cover several)

- **`release_version`:** accepts `v1.2.3` and `v1.2.3-rc.1`. Rejects `1.2.3`, `v1.2`, `v1.2.3.4`,
  `v1.2.3+b` and a 129-char tag. `order_key` ranks a full release above its prerelease.
- **Class-definition checks:** each of checks 1–8 raises `TypeError` (one test each). A failed definition
  leaves the registry unchanged, so a corrected class with the same name then succeeds.
- **`job_keys`:**
  - A two-field test type with `subject=("a",)` gets a subject-only lock and an all-fields queueing lock.
  - Keys are injective: values containing `,` `"` `]` `\\` never collide (hypothesis over two-field payloads).
  - Field-less jobs get constant keys.
  - `asset_key(FetchOsImage(tag=t)) == AssetKey(OS_IMAGE, t)`, and `lock` embeds the same value.
- **Job validation:** `FetchOsImage(tag="1.0")` and `FetchPackage(sha256="XYZ")` raise `ValidationError`. Jobs are
  frozen and reject extra fields.
- **`Delivery`:** `every=7min`, `retry=(0s,)` and `priority=101` raise.
- **Construction invariants:** each of these raises:
  - `Failed(terminal=True, retry_after=1s)`
  - `Candidates((), pinned=False)`
  - a pinned `Candidates` with two jobs
  - a `Candidates` mixing asset kinds
  - `Asset(references=())`
- **`handler_job_type`:** a correct handler returns its job type. A wrong return hint, a missing hint, or an unregistered
  job type raises `TypeError`.
- **`SettledHandle.wait`:** returns its outcome immediately, whatever the timeout.
- **P0.2 `PgTransactions`:** test the state machine with a stub `Database` that exposes `transaction()`.
  - The commit path ends `committed`.
  - A raising body ends `rolled_back` and re-raises.
  - `pg_connection(FakeTransaction())` raises `TypeError`.
- **P0.2 `RecordingPublisher`:** honours PB1–PB9. This test file is the fake half of Lane A's conformance suite;
  Lane A later parametrizes the same cases over the real adapter.
- **`lint-imports`:** passes with all five new packages present.
