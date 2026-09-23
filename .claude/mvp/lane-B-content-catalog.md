# Lane B — content catalog & release policy (`central.content_catalog` + its repo)

Design: §3 (the Catalog row), §5 (catalog entries, desired set), §6(a)/(b), §9 decision 4, and the §10.4
`ContentCatalog` row. Kernel: `P0-kernel.md`. This lane PORTS the release policy that lives
in `central/netboot_base.py:350-666` and `central/app_releases.py`. It keeps that behaviour and moves it behind ports.
The legacy files stay in place until P2 deletes them.

## Owns (create)
- `central/content_catalog/ports.py`, `boot_policy.py`, `catalog.py`, `sync.py`
- `central/infra/catalog_records.py`
- `tests/test_content_catalog_boot_policy.py`, `tests/test_content_catalog_catalog.py`,
  `tests/test_content_catalog_sync.py`, `tests/test_infra_catalog_records.py` (DB; skips locally),
  and `tests/catalog_fakes.py` (in-memory `ReleaseRecords`/`DeviceRecords`)

## Consumes
The kernel: `AssetJob`, `FetchOsImage`, `FetchPackage`, `SyncReleases`, `Prefetch`, `Candidates`, `Unknown`,
the request types, `ContentCatalog`, `AssetRecords`, `AssetKey`, `AssetKind`, `AssetReference`,
`OriginLocator`, `ReleaseOrigin`, `PublishedRelease`, `Publisher`, `Transactions`, `release_version`,
and the failures. From legacy code it may import only stdlib-only modules: `contracts.equipment.equipment_device_id`
and `contracts.time.Clock`. It may NOT import `central.netboot_base` or `central.app_releases`; they pull in `psycopg`.

## Must not touch
`pyproject.toml`, `tests/fakes/`, other `central/infra/*`, `central/app.py`, `media/`, legacy
`central/*.py`, and every migration. It uses the existing tables unchanged: `app_releases`, `app_release_policy`,
`app_release_poll`, `devices`, and `bindings`.

## Provides (frozen signatures)

```python
# ports.py
BootOutcome: TypeAlias = Literal["pending", "healthy", "failed"]
@dataclass(frozen=True, slots=True)
class ReleaseRow:
    tag: str; is_prerelease: bool
    package: OriginLocator | None          # the .deb (asset_url/asset_sha256/asset_size)
    os_image: OriginLocator | None         # the base tarball (base_tarball_url/_sha256/_size)
@dataclass(frozen=True, slots=True)
class DeviceRow:
    device_id: str; serial: str | None; attached_tag: str | None; known_good_tag: str | None
    last_served_tag: str | None; boot_outcome: BootOutcome | None; failed_tag: str | None
    last_served_at: float | None; retired: bool
@dataclass(frozen=True, slots=True)
class DeviceUpdate:
    failed_tag: str | None                 # the new devices.failed_tag
    mark_boot_failed: bool                 # also set boot_outcome='failed'

class ReleaseRecords(Protocol):
    def get(self, tx: Transaction, tag: str) -> ReleaseRow | None: ...
    def all(self, tx: Transaction) -> tuple[ReleaseRow, ...]: ...
    def upsert(self, tx: Transaction, release: PublishedRelease, *, now: float) -> ReleaseRow | None: ...
        # returns the PREVIOUS row (None when new); writes tag/semver columns/is_prerelease/asset_*/base_*;
        # on insert mirror_state = 'discovered' if package else 'undeployable' (legacy NOT NULL column; never read)
    def promoted_tag(self, tx: Transaction) -> str | None: ...
    def set_promoted(self, tx: Transaction, tag: str) -> None: ...
    def load_etag(self, tx: Transaction) -> str | None: ...
    def store_etag(self, tx: Transaction, etag: str | None) -> None: ...
    def bound_player_count(self, tx: Transaction) -> int: ...   # count(DISTINCT player_id) FROM bindings

class DeviceRecords(Protocol):
    def lock(self, tx: Transaction, device_id: str, serial: str, *, now: float) -> DeviceRow: ...
        # upsert first_seen/last_seen/serial, then SELECT ... FOR UPDATE (E1)
    def active(self, tx: Transaction) -> tuple[DeviceRow, ...]: ...        # retired_at IS NULL
    def get(self, tx: Transaction, device_id: str) -> DeviceRow | None: ...
    def apply(self, tx: Transaction, device_id: str, update: DeviceUpdate) -> None: ...
    def record_served(self, tx: Transaction, device_id: str, tag: str, *, now: float) -> None: ...
        # last_served_tag=tag, boot_outcome='pending', last_served_at=now
    def set_pin(self, tx: Transaction, device_id: str, tag: str | None) -> bool: ...  # False: no such device
    def sweep_failed_boots(self, tx: Transaction, *, served_before: float) -> int: ...
        # the exact SQL of netboot_base.sweep_failed_boots (E2/E2b semantics)

# boot_policy.py — PURE (no I/O); the ported state machine
@dataclass(frozen=True, slots=True)
class BootChoice:
    tags: tuple[str, ...]                  # candidate tags, preference order, non-empty
    pinned: bool
    update: DeviceUpdate | None            # None = no device write
def newest(tags: Iterable[str]) -> str | None: ...        # max by release_version(t).order_key(); invalid tags ignored
def choose_base(device: DeviceRow | None, *, frontier: str | None,
                bootstrap: str | None) -> BootChoice | None: ...   # None ⇒ Unknown("no_release")

# catalog.py
@dataclass(frozen=True, slots=True)
class DevicePackage:
    tag: str; version: str; sha256: str; size: int         # version == tag (was the app_packages label)
@dataclass(frozen=True, slots=True)
class ManifestRefusal:
    code: Literal["app_manifest_unresolved", "app_unconfigured", "app_manifest_undeployable"]
class CatalogError(Exception):
    def __init__(self, code: str, kind: Literal["not_found", "conflict", "invalid"]) -> None: ...
@dataclass(frozen=True, slots=True)
class ReleaseView:
    tag: str; is_prerelease: bool; deployable: bool; promoted: bool; has_os_image: bool
@dataclass(frozen=True, slots=True)
class NetbootView:
    frontier: str | None; devices: tuple[DeviceRow, ...]

class ReleaseCatalog:                      # implements kernel ContentCatalog
    def __init__(self, *, releases: ReleaseRecords, devices: DeviceRecords,
                 transactions: Transactions, publisher: Publisher, clock: Clock) -> None: ...
    async def resolve(self, request: ContentRequest) -> Resolution: ...
    async def desired_assets(self) -> frozenset[AssetJob]: ...
    async def record_served(self, request: NetbootBaseRequest, job: FetchOsImage) -> None: ...
    async def device_package(self, serial: str | None) -> DevicePackage | ManifestRefusal: ...
    async def promoted_package(self) -> DevicePackage | ManifestRefusal: ...
    async def pin(self, device_id: str, tag: str) -> None: ...         # CatalogError
    async def unpin(self, device_id: str) -> None: ...                 # CatalogError
    async def promote(self, tag: str) -> None: ...                     # CatalogError
    async def refresh(self) -> None: ...
    async def releases_view(self) -> tuple[ReleaseView, ...]: ...      # semver DESC
    async def netboot_view(self) -> NetbootView: ...
    def sanitize_serial(self, serial: str | None) -> str | None: ...   # ported _SAFE_SERIAL; the route logs this value

# sync.py
class SyncReleasesHandler:                 # Handler[SyncReleases, None]
    def __init__(self, *, origin: ReleaseOrigin, releases: ReleaseRecords, devices: DeviceRecords,
                 assets: AssetRecords, transactions: Transactions, publisher: Publisher,
                 catalog: ReleaseCatalog, clock: Clock,
                 pending_health_timeout: timedelta = timedelta(minutes=15)) -> None: ...
    async def handle(self, job: SyncReleases) -> None: ...

# central/infra/catalog_records.py
class PgReleaseRecords: ...                # implements ReleaseRecords (SQL over existing tables)
class PgDeviceRecords: ...                 # implements DeviceRecords
```
Every public async method runs its blocking work with `asyncio.to_thread` and opens its own
`transactions.begin()`. Nothing blocks the event loop.

## Behaviour (acceptance criteria)

**B1 `choose_base`.** This is a pure port of `_resolve_recovery_aware` (`netboot_base.py:455-504`). Let `desired =
frontier or bootstrap`.
- A pin wins: `tags=(pin,)`, `pinned=True`. If `failed_tag` is set, `update` clears it.
- DETECT: `last_served_tag == desired` and `boot_outcome == 'pending'` → fence `desired`
  (`DeviceUpdate(desired, True)`).
- RELEASE: `failed_tag` set and `≠ desired` → clear it; serve `desired`.
- RECOVER: `failed_tag == desired` and a known-good exists → `tags=(known_good,)`. The fence stays.
- Fenced with no known-good → `tags=(desired,)` (boot-loops until an operator pins; unchanged).
- NEW (design §10.4 "unpinned may substitute"): in every other unpinned case,
  `tags=(desired, known_good)` when known-good exists and differs. `AssetReader` serves the first one
  that is present. A substitute boot records the substitute as served, so DETECT stays quiet.
- `device=None` (the serial is absent or invalid) → `tags=(desired,)` and no update.
- `desired is None` and no pin → `None`.

Port every scenario in `tests/test_netboot_base_recovery.py` and `tests/test_netboot_base_tracer.py`
into pure `choose_base` tests. Each needs an assertion on `tags` and on `update`.

**B2 `resolve`.**
- `NetbootBaseRequest`: one transaction does the following.
  - Sanitize the serial and derive `device_id = equipment_device_id("pi", serial)` (`netboot_base.py:209-218`).
  - `devices.lock`.
  - `frontier = newest(known_good of active devices)`.
  - `bootstrap = newest(non-prerelease releases with os_image)`.
  - `choose_base`, then apply the `update`.
  - Keep only the tags whose release has `os_image`, and map each to `FetchOsImage(tag)`.

  If nothing is left → `Unknown("no_release")` (404, decision 4). A 503 miss writes nothing else;
  `record_served` is called only after a 200.
- `PackageRequest(sha)` → `Candidates((FetchPackage(sha),), pinned=True)` iff some release's
  `package.sha256 == sha`, else `Unknown("unknown_package")`.

**B3 `desired_assets`.** It returns the union of:
- `FetchOsImage` for: active pins, active known-goods, and `frontier or bootstrap`.
- `FetchPackage` for: each of those tags' `.deb`, and the promoted tag's `.deb`.

Tags without the matching locator are skipped.

**B4 `device_package` / `promoted_package`.**
- `device_package`: the device's `last_served_tag` → that release's `.deb`.
  - no carried tag → `app_manifest_unresolved`
  - the release has no `.deb` → `app_manifest_undeployable`
- `promoted_package`: the promoted tag → its `.deb`, or `app_unconfigured`.

The manifest is NO LONGER gated on the `.deb` bytes being present. The bytes route reads through the cache
(this changes `app.py:533-564`).

**B5 Operator actions.** Each runs in one transaction.
- `pin`:
  - unknown tag → `CatalogError("release_not_found", "not_found")` with no write
  - unknown device → `CatalogError("device_not_found", "not_found")` with no write
  - otherwise it publishes `FetchOsImage(tag)` and, if the release has a `.deb`, `FetchPackage(sha)`.
    Both use `within=tx, retry_terminal=True` (an operator action, decision 3).
- `unpin`: `device_not_found` as above.
- `promote`:
  - unknown tag → `release_not_found`
  - no `.deb` → `CatalogError("release_undeployable", "conflict")`
  - otherwise `set_promoted` + `FetchPackage(sha)` with `retry_terminal=True`
- `refresh`: `publish_now(SyncReleases(), retry_terminal=True)`.

Every one of these also publishes `Prefetch()` (on change, §4).

**B6 `SyncReleasesHandler.handle`.**
1. Read the etag, then `origin.list_releases(etag=...)`. An origin failure propagates; the runtime records it.
2. Unless `unchanged`, give each release its own transaction:
   - `prev = upsert`
   - `assets.reference` an `os-image` key (owner=tag, locator=os_image, expected None/None) and a `player-deb` key
     (owner=tag, locator=package, expected_size=package.size, expected_sha256=package.sha256)
   - If `prev` had a different `.deb` sha, retire `(player-deb, old_sha)` owner=tag (a re-cut).
   - If `prev` had an os_image and the new release has none, retire `(os-image, tag)`.
   - Collect the keys whose `reference` returned True.
   - Then `store_etag`.
3. The tail transaction does four things.
   - `sweep_failed_boots(served_before=now - pending_health_timeout)`: the poll-tail duty with no §4 home.
   - Auto-promote. If `promoted_tag is None` or `bound_player_count == 0`, promote the newest non-prerelease
     release that has a `.deb` (it replaces boot-time `app_release_boot._autopull_deb`, "cached==0" becomes
     "nothing promoted").
   - `publish(fetch, within=tx, retry_terminal=True)` for every changed key that is in `desired_assets()`
     (§10.2 "a sync that changed facts").
   - `publish(Prefetch(), within=tx)`.
4. Withdrawal (retiring tags that are gone upstream) is NOT in the MVP.

**B7 Repositories (DB tests, CI).** `PgReleaseRecords`/`PgDeviceRecords` round-trip every
method against a migrated schema. Port the SQL assertions from `test_netboot_base_pin.py` and the
sweep cases in `test_netboot_base_recovery.py`. A fake transaction handed to them raises `TypeError`.

## Beads (serial inside the lane)
| Bead | Scope |
| --- | --- |
| B-1 | `ports.py`, `boot_policy.py`, `catalog_fakes.py`, `test_content_catalog_boot_policy.py` |
| B-2 | `catalog.py` + `test_content_catalog_catalog.py` (B2–B5 against the fakes and `RecordingPublisher`) |
| B-3 | `sync.py` + `test_content_catalog_sync.py` (with `FakeReleaseOrigin`, `InMemoryAssetRecords`) |
| B-4 | `central/infra/catalog_records.py` + `test_infra_catalog_records.py` |
