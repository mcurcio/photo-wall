# Lane C — assets & cache: read path, production, fetch handlers (`central.assets` + its repo)

Design: §1 rules 1–2, §5 (the Asset row), §6(b)–(e), §10.2 ("A player asks for photo X"; the `read` sketch), and
§10.4 (`AssetReader`, `CacheStore`, `AssetProduction`). Kernel: `P0-kernel.md`. This lane ports the OS
extraction from `central/netboot_base.py:672-736` and the hardened open from `central/artifact_io.py`
(reuse that module: it is stdlib-only).

## Owns (create)
- `central/assets/layout.py`, `store.py`, `production.py`, `os_image.py`, `handlers.py`, `reader.py`
- `central/infra/asset_records.py`
- `central/migrations/021_assets.sql`
- `tests/test_assets_store.py`, `tests/test_assets_os_image.py`, `tests/test_assets_production.py`,
  `tests/test_assets_handlers.py`, `tests/test_assets_reader.py`, `tests/test_infra_asset_records.py`
  (DB; skips locally)

## Consumes
The kernel: `AssetKey`, `AssetKind`, `AssetReady`, `Asset`, `AssetReference`, `OriginLocator`, `AssetJob`,
`FetchOsImage`, `FetchPackage`, `Prefetch`, `asset_key`, `Candidates`, `ContentCatalog`, `AssetRecords`,
`ReleaseOrigin`, `Publisher`, `Ready`, `Failed`, `Pending`, `Transactions`, and the failures. It also consumes
`central.cache_layout.cache_root`/the subdir constants, `central.artifact_io.open_regular`,
`contracts.release.MAX_ROOTFS_BYTES`, and `contracts.time.Clock`. In `central/infra/asset_records.py` only, it consumes
`pg_connection`. Tests use `fakes.*`.

## Must not touch
`pyproject.toml`, `tests/fakes/`, other `central/infra/*`, `central/app.py`, `media/`, legacy
`central/*.py` (read them, do not edit them), and other migrations.

## Migration `021_assets.sql`
```sql
CREATE TABLE assets (
    kind TEXT NOT NULL CHECK (kind IN ('os-image','player-deb')),
    identity TEXT NOT NULL CHECK (length(identity) BETWEEN 1 AND 256),
    produced_size BIGINT CHECK (produced_size > 0),
    produced_sha256 TEXT CHECK (produced_sha256 ~ '^[0-9a-f]{64}$'),
    last_served_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, identity),
    CHECK ((produced_size IS NULL) = (produced_sha256 IS NULL))
);
CREATE TABLE asset_references (
    kind TEXT NOT NULL, identity TEXT NOT NULL,
    owner TEXT NOT NULL CHECK (length(owner) BETWEEN 1 AND 128),
    locator_url TEXT NOT NULL,
    locator_sha256 TEXT CHECK (locator_sha256 ~ '^[0-9a-f]{64}$'),
    locator_size BIGINT CHECK (locator_size > 0),
    expected_size BIGINT CHECK (expected_size > 0),
    expected_sha256 TEXT CHECK (expected_sha256 ~ '^[0-9a-f]{64}$'),
    added_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, identity, owner),
    FOREIGN KEY (kind, identity) REFERENCES assets (kind, identity) ON DELETE CASCADE
);
-- Warm rollout: seed from the legacy catalog so files already on disk keep serving.
--   os-image refs  ← app_releases rows with base_tarball_url/sha256/size (owner = tag)
--   player-deb refs← app_releases rows with asset_url/sha256/size     (owner = tag, expected = asset_*)
--   produced facts ← base_cache.squashfs_sha256/size (os-image, identity = tag)
--                  ← app_packages.sha256/size        (player-deb, identity = sha256)
-- created_at/added_at = EXTRACT(EPOCH FROM now()). Only rows that have a reference get facts.
```
The disk layout keeps today's file names, so existing cache files are reused. The OS image is
`os-images/base-<tag>.squashfs` and the `.deb` is `apps/app-<sha256>.deb`.

## Provides (frozen signatures)

```python
# layout.py
TEMP_PREFIX: Final = ".tmp-"
class CacheLayout:
    def __init__(self, root: Path) -> None: ...           # root = cache_layout.cache_root() in production
    def directory(self, kind: AssetKind) -> Path: ...     # os-image → root/"os-images"; player-deb → root/"apps"
    def path(self, key: AssetKey) -> Path: ...            # base-{identity}.squashfs | app-{identity}.deb

# store.py
@dataclass(frozen=True, slots=True)
class OpenedFile:
    fd: int; size: int                                    # the caller owns fd
class CacheStore:                                         # filesystem only; tests run it on tmp_path
    def __init__(self, layout: CacheLayout) -> None: ...
    def open(self, key: AssetKey, facts: AssetReady) -> OpenedFile | None: ...
        # open_regular(path, expected_size=facts.size); None when absent/invalid (never raises for those)
    def present(self, key: AssetKey, facts: AssetReady) -> bool: ...   # lstat: regular file, size match
    def temp_path(self, key: AssetKey) -> Path: ...       # unique NON-EXISTENT path in the kind dir (dir created)
    def measure(self, path: Path) -> AssetReady: ...      # streaming sha256 + size (blocking)
    def install(self, temp: Path, key: AssetKey) -> None: ...   # os.replace + fsync(dir)
    def discard(self, path: Path) -> None: ...            # unlink(missing_ok=True)

# os_image.py
MAX_SQUASHFS_BYTES: Final = MAX_ROOTFS_BYTES
def extract_squashfs(tarball: Path, into: Path) -> None: ...
    # blocking. Reads ONLY photo-wall-base/photo-wall-base.squashfs + photo-wall-base/SHA256SUMS (first
    # occurrence), refuses non-regular members, bounds both, verifies the squashfs against SHA256SUMS,
    # writes `into` exclusively. Hostile/inconsistent archive → TerminalFailure("base_member_missing" |
    # "base_member_not_file" | "base_too_large" | "base_sums_too_large" | "base_sums_no_squashfs" |
    # "base_digest_mismatch"); `into` never left behind on failure.

# production.py
WriteFn: TypeAlias = Callable[[Path, Asset], Awaitable[None]]
class AssetProduction:                                    # shared by every fetch handler (§10.4)
    def __init__(self, *, store: CacheStore, records: AssetRecords, transactions: Transactions) -> None: ...
    async def produce(self, job: AssetJob, write: WriteFn) -> AssetReady: ...

# handlers.py   (job types imported at runtime: handler_job_type reads the hints)
MAX_TARBALL_BYTES: Final = MAX_ROOTFS_BYTES
MAX_PACKAGE_BYTES: Final = 1024**3
class FetchOsImageHandler:
    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin, store: CacheStore) -> None: ...
    async def handle(self, job: FetchOsImage) -> AssetReady: ...
class FetchPackageHandler:
    def __init__(self, *, production: AssetProduction, origin: ReleaseOrigin) -> None: ...
    async def handle(self, job: FetchPackage) -> AssetReady: ...
class PrefetchHandler:
    def __init__(self, *, catalog: ContentCatalog, records: AssetRecords, store: CacheStore,
                 transactions: Transactions, publisher: Publisher) -> None: ...
    async def handle(self, job: Prefetch) -> None: ...

# reader.py
@dataclass(frozen=True, slots=True)
class Opened:
    job: AssetJob; fd: int; size: int; sha256: str        # caller owns fd; sha256 → the Digest header
@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: str; retry_after_seconds: int                 # → 503 + Retry-After
class SlotsFull(Exception): ...
class WaiterSlots:                                        # bulkhead; stdlib only (anyio is not a declared dep)
    def __init__(self, capacity: int) -> None: ...
    def claim(self) -> AbstractContextManager[None]: ...  # non-blocking; raises SlotsFull at capacity
    @property
    def in_use(self) -> int: ...
class AssetReader:
    def __init__(self, *, store: CacheStore, records: AssetRecords, transactions: Transactions,
                 publisher: Publisher, slots: WaiterSlots, clock: Clock,
                 wait_timeout: timedelta = timedelta(seconds=30),
                 touch_interval: timedelta = timedelta(minutes=5)) -> None: ...
    async def read(self, candidates: Candidates) -> Opened | Unavailable: ...

# central/infra/asset_records.py
class PgAssetRecords: ...                                 # implements kernel AssetRecords (SQL above)
```

## Behaviour (acceptance criteria)

**C1 `AssetProduction.produce`.**
1. `get` the asset (in a thread, own transaction). If it is missing → `TerminalFailure("unknown_asset")`.
2. If the file is present, `measure` it.
   - It equals `produced` → return `produced`. No write.
   - There is no `produced`, and it equals the newest reference's `expected_sha256`/`expected_size` → return the measured facts.
   - Otherwise → discard it and re-produce. An os-image with no `produced` has nothing to check against, so it
     always re-produces. This is what makes a crash between the rename and the outcome write harmless.
3. Produce the file:
   - `temp = temp_path`, then `await write(temp, asset)`, then `measure(temp)` in a thread.
   - Check the result:
     - It differs from `produced` → `TerminalFailure("not_reproducible")`.
     - Else, it differs from a reference's expected facts → `TerminalFailure("digest_mismatch")`.
   - `install` in a thread.
   - Return the facts.
4. `temp` is discarded on every non-installed exit, including cancellation. `produce` writes NO record:
   the runtime writes `produced` and the outcome.

**C2 Fetch handlers.**
- `FetchOsImageHandler`:
  - `write` downloads `references[0].locator` to a second `temp_path` with
    `max_bytes = locator.size or MAX_TARBALL_BYTES`.
  - Then `asyncio.to_thread(extract_squashfs, tar, temp)`. Extraction runs OFF the event loop (§8 fix,
    `netboot_base.py:1030`).
  - The tarball temp is always discarded.
- `FetchPackageHandler`: `write` tries the references newest first.
  - `OriginRejected` → try the next reference.
  - `OriginUnavailable` → remember it and try the next.
  - After the last reference: raise the last `OriginUnavailable` if there was one, else
    `TerminalFailure("all_references_rejected")`.
  - Two tags that ship one sha share one file and one producer.
- `PrefetchHandler`: publishes `publish_now(job)` for each `desired_assets()` job whose asset exists and is
  not `present(key, produced)` (a missing `produced` counts as absent). It uses no `retry_terminal`, and it
  never waits.

**C3 `AssetReader.read`.** This is the §10.2 sketch, made concrete.
1. In one thread call with one read transaction: for each candidate in order, `get`. The first one with
   `produced` whose `store.open` succeeds → `Opened`. This path publishes nothing (flow (b)).
2. None is open → `slots.claim()`. `SlotsFull` → `Unavailable("busy", 5)`.
3. `publish_now(jobs[0], retry_terminal=True)` (decision 3), then `wait(timeout=wait_timeout)`.
   - `Ready(facts)` → `store.open(key, facts)`, or `Unavailable("absent_after_ready", 1)` if the open fails.
   - `Failed(transient)` → `Unavailable(reason, max(1, ceil(retry_after)))`.
   - `Failed(terminal)` → `Unavailable(reason, 30)`.
   - `Pending` → `Unavailable("timeout", 5)`.
4. `last_served_at` is touched at most once per `touch_interval` per key per process, after an `Opened`.
   A touch failure is logged and never fails a serve.
5. Cancellation (the route's disconnect watcher) releases the slot. It closes any fd opened but not returned,
   and it never cancels the job.
6. A pinned candidate never yields a different job, because `Candidates` guarantees one.

**C4 `extract_squashfs`.** Port every hostile-archive case from `tests/test_netboot_base.py`
(traversal, symlink, device, duplicate names, oversize, wrong digest, missing member) as unit tests.

**C5 `PgAssetRecords` (DB, CI).**
- `reference` is idempotent and reports whether anything changed.
- `retire` of the last reference deletes the row (cascade).
- `record_produced` is write-once: equal facts → no-op, different facts → `ProducedFactsConflict`.
- `get` orders references by `added_at DESC, owner DESC`.
- The 021 backfill seeds a pre-migrated schema correctly.

## Beads (serial inside the lane)
| Bead | Scope |
| --- | --- |
| C-1 | `layout.py`, `store.py`, `os_image.py`, `test_assets_store.py`, `test_assets_os_image.py` |
| C-2 | `production.py`, `handlers.py`, `test_assets_production.py`, `test_assets_handlers.py` |
| C-3 | `reader.py`, `test_assets_reader.py` (with `RecordingPublisher` settling via `record_outcome`) |
| C-4 | migration 021, `central/infra/asset_records.py`, `test_infra_asset_records.py` |
