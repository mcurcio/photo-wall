"""The tracer of rule 1 (docs/central-idempotent-jobs.md §4, §5, §13): an OS image is named by
the sha256 of its base tarball, so jobs may run in any order.

Real PostgreSQL (the `registry` fixture; CI runs it), a fake origin, real tarballs, the real
release sync, `AssetProduction` with the real OS-image handler, and the real boot route over
`AssetReader`. A worker's run is `FetchOsImageHandler.handle` plus the `record_produced` the
runtime writes after it. T7 migrates a schema at 027 across 028.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import timedelta

import pytest
from content_db import Reads, RecordingTransactions, insert_device, schema_before
from fakes.origin import FakeReleaseOrigin
from fakes.publisher import RecordingPublisher
from fastapi.testclient import TestClient
from support.github_release import real_tarball
from test_content_routes_http import ADMIN, StubDatabase

from central.app import create_app
from central.assets.handlers import FetchOsImageHandler
from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.assets.production import AssetProduction
from central.assets.reader import AssetReader, WaiterSlots
from central.assets.store import CacheStore
from central.content_catalog.catalog import NetbootCandidates, ReleaseCatalog
from central.content_catalog.sync import SyncReleasesHandler
from central.content_wiring import ContentServices
from central.db import Database
from central.health.probe import PodProbe
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.infra.transactions import pg_connection
from central.kernel.assets import AssetKey, AssetKind, AssetReady, OriginLocator
from central.kernel.job_types import FetchOsImage, SyncReleases
from central.kernel.jobs import asset_key
from central.kernel.ports import (
    NetbootBaseRequest,
    PublishedRelease,
    ReleaseListing,
    UpstreamVersion,
)
from central.netboot_base import SERIAL_HEADER
from contracts.equipment import equipment_device_id
from contracts.time import ManualClock

V1, V2 = "v1.0.0", "v1.1.0"
SERIAL = "10000000c0ffee77"
DEVICE_ID = equipment_device_id("pi", SERIAL.encode())


class Build:
    """One base tarball: its bytes, its sha (the OS image's key) and the squashfs inside."""

    def __init__(self, label: str) -> None:
        self.squashfs = f"squashfs {label} ".encode() * 256
        self.tarball, self.sha, _ = real_tarball(self.squashfs)
        self.job = FetchOsImage(tarball_sha256=self.sha)
        self.key = AssetKey(AssetKind.OS_IMAGE, self.sha)
        self.facts = AssetReady(size=len(self.squashfs),
                                sha256=hashlib.sha256(self.squashfs).hexdigest())

    def locator(self, url: str) -> OriginLocator:
        return OriginLocator(url, sha256=self.sha, size=len(self.tarball))


S1, S2 = Build("S1"), Build("S2")


def url(tag: str) -> str:
    """A release's base tarball URL: a re-cut re-uploads under the same one (`--clobber`)."""
    return f"https://example.test/{tag}/photo-wall-base.tar.gz"


def published(tag: str, build: Build, at: int = 1) -> PublishedRelease:
    """`tag` shipping `build`, observed at upstream version `at`: a re-cut is a newer one."""
    return PublishedRelease(tag, False, None, "no_player_asset", build.locator(url(tag)),
                            UpstreamVersion(float(at), 1))


class HeldOrigin(FakeReleaseOrigin):
    """The fake origin, whose download of a held sha stops once its bytes are written, until
    released: a worker paused mid-download (design §5)."""

    def __init__(self) -> None:
        super().__init__(ReleaseListing((), None, unchanged=True), {})
        self.held: dict[str, asyncio.Event] = {}
        self.reached: dict[str, asyncio.Event] = {}

    def hold(self, sha: str) -> None:
        self.held[sha] = asyncio.Event()
        self.reached[sha] = asyncio.Event()

    async def download(self, locator, into, *, max_bytes):
        await super().download(locator, into, max_bytes=max_bytes)
        if locator.sha256 in self.held:
            self.reached[locator.sha256].set()
            await self.held[locator.sha256].wait()

    def fetched(self, build: Build) -> int:
        return sum(1 for locator in self.downloads if locator.sha256 == build.sha)


class World:
    def __init__(self, registry, tmp_path) -> None:
        self.db = registry.db
        self.clock = ManualClock(1000.0)
        self.assets = PgAssetRecords(self.clock)
        self.transactions = RecordingTransactions(registry.db)
        self.reads = Reads(RecordingTransactions(registry.db))
        self.publisher = RecordingPublisher(self.clock, self.assets, self.transactions)
        self.store = CacheStore(CacheLayout(tmp_path / "cache"))
        self.catalog = ReleaseCatalog(
            releases=PgReleaseRecords(), devices=PgDeviceRecords(),
            stored=DiskStoredAssets(records=self.assets, store=self.store),
            transactions=self.transactions, publisher=self.publisher, clock=self.clock)
        self.origin = HeldOrigin()
        self.sync_handler = SyncReleasesHandler(
            origin=self.origin, releases=PgReleaseRecords(), devices=PgDeviceRecords(),
            assets=self.assets, transactions=self.transactions, publisher=self.publisher,
            catalog=self.catalog, clock=self.clock)
        self.fetch_handler = FetchOsImageHandler(
            production=AssetProduction(store=self.store, records=self.assets,
                                       transactions=self.transactions),
            origin=self.origin, store=self.store)
        reader = AssetReader(store=self.store, records=self.assets,
                             transactions=self.transactions, publisher=self.publisher,
                             slots=WaiterSlots(4), clock=self.clock,
                             wait_timeout=timedelta(seconds=1))
        database = StubDatabase()
        self.app = create_app(database, self.clock, ADMIN, content=ContentServices(
            catalog=self.catalog, reader=reader, probe=PodProbe(database.healthy), feed=None))
        self._etag = 0

    # -- the release origin and its sync ----------------------------------------------------------

    def upstream(self, *releases: PublishedRelease, serving: dict[str, Build]) -> None:
        """What GitHub lists now, and which tarball each URL answers (unmapped: 404)."""
        self._etag += 1
        self.origin.listing = ReleaseListing(tuple(releases), f"e{self._etag}", unchanged=False)
        self.origin.blobs = {at: build.tarball for at, build in serving.items()}

    async def sync(self) -> None:
        await self.sync_handler.handle(SyncReleases())

    def list(self, *releases: PublishedRelease, serving: dict[str, Build]) -> None:
        self.upstream(*releases, serving=serving)
        asyncio.run(self.sync())

    # -- a worker's run ---------------------------------------------------------------------------

    async def run_fetch(self, build: Build) -> AssetReady:
        """`handle`, then what the runtime writes: the produced facts."""
        facts = await self.fetch_handler.handle(build.job)
        with self.transactions.begin() as tx:
            self.assets.record_produced(tx, build.key, facts)
        return facts

    def fetch(self, build: Build) -> AssetReady:
        return asyncio.run(self.run_fetch(build))

    # -- reads ------------------------------------------------------------------------------------

    def os_references(self, owner: str) -> list[str]:
        """The OS-image keys `owner` references."""
        with self.transactions.begin() as tx:
            rows = pg_connection(tx).execute(
                "SELECT identity FROM asset_references WHERE kind='os-image' AND owner=%s "
                "ORDER BY identity", (owner,)).fetchall()
        return [row["identity"] for row in rows]

    def os_asset_rows(self) -> int:
        with self.transactions.begin() as tx:
            return pg_connection(tx).execute(
                "SELECT count(*) AS n FROM assets WHERE kind='os-image'").fetchone()["n"]

    def file(self, build: Build) -> bytes | None:
        path = self.store.layout.path(build.key)
        return path.read_bytes() if path.exists() else None

    def os_files(self) -> list[str]:
        directory = self.store.layout.directory(AssetKind.OS_IMAGE)
        return sorted(p.name for p in directory.iterdir()
                      if not p.name.startswith(TEMP_PREFIX)) if directory.exists() else []

    def boot(self, serial: str = SERIAL):
        with TestClient(self.app) as client:
            return client.get("/v1/netboot/base", headers={SERIAL_HEADER: serial})


def digest(data: bytes) -> str:
    return "sha-256=" + base64.b64encode(hashlib.sha256(data).digest()).decode()


@pytest.fixture
def world(registry, tmp_path):
    return World(registry, tmp_path)


def test_t1_a_recut_moves_the_reference_to_the_new_key_and_boots_it(world):
    world.list(published(V1, S1), serving={url(V1): S1})
    assert world.fetch(S1) == S1.facts
    world.list(published(V1, S2, at=2), serving={url(V1): S2})  # re-cut: same URL, new bytes

    assert world.os_references(V1) == [S2.sha]  # v1 references only (os-image, S2)
    assert world.reads.facts(S1.key) == S1.facts  # the old key keeps its facts
    assert world.fetch(S2) == S2.facts
    response = world.boot()
    assert response.status_code == 200
    assert response.content == S2.squashfs
    assert response.headers["digest"] == digest(S2.squashfs)
    assert world.reads.device(DEVICE_ID).last_served_tag == V1


def test_t2_a_zombie_on_the_old_key_cannot_touch_the_new_one(world):
    world.list(published(V1, S1), serving={url(V1): S1})

    async def scenario() -> None:
        world.origin.hold(S1.sha)
        zombie = asyncio.create_task(world.run_fetch(S1))  # worker A: S1's bytes, then paused
        await world.origin.reached[S1.sha].wait()
        world.upstream(published(V1, S2, at=2), serving={url(V1): S2})
        await world.sync()  # worker B: v1 is re-cut to S2 ...
        assert await world.run_fetch(S2) == S2.facts  # ... and S2 is produced
        world.origin.held[S1.sha].set()  # A resumes, long after
        assert await zombie == S1.facts  # its own name, its own facts

    asyncio.run(scenario())
    assert world.file(S2) == S2.squashfs
    assert world.reads.asset(S2.key).produced == S2.facts
    assert world.file(S1) == S1.squashfs and world.reads.facts(S1.key) == S1.facts
    response = world.boot()
    assert response.status_code == 200 and response.content == S2.squashfs
    assert response.headers["digest"] == digest(S2.squashfs)


def test_t3_a_flip_flop_keeps_the_facts_and_downloads_once(world):
    world.list(published(V1, S2), serving={url(V1): S2})
    assert world.fetch(S2) == S2.facts
    world.list(published(V1, S1, at=2), serving={url(V1): S1})
    world.list(published(V1, S2, at=3), serving={url(V1): S2})
    assert world.fetch(S2) == S2.facts  # the sync's fetch finds the verified file ...
    assert world.origin.fetched(S2) == 1  # ... and downloads nothing
    assert world.reads.asset(S2.key).produced == S2.facts  # the facts survived the interlude


def test_t4_two_tags_sharing_a_tarball_are_one_asset_and_one_candidate(world):
    world.list(published(V1, S1), published(V2, S1), serving={url(V1): S1, url(V2): S1})
    assert world.os_asset_rows() == 1
    assert sorted(world.reads.owners(S1.key)) == [V1, V2]
    resolution = asyncio.run(world.catalog.resolve(NetbootBaseRequest(SERIAL)))
    assert resolution == NetbootCandidates((S1.job,), pinned=False, tags=(V2,))  # the preferred
    world.fetch(S1)
    assert world.os_files() == [f"base-{S1.sha}.squashfs"]


def test_t5_a_rejected_newest_reference_falls_back_to_another_tags_url(world):
    # Equal added_at: the newest reference is v1.1.0's (owner DESC). Its URL answers 404.
    world.list(published(V1, S1), published(V2, S1), serving={url(V1): S1})
    assert [ref.owner for ref in world.reads.asset(S1.key).references] == [V2, V1]
    assert world.fetch(S1) == S1.facts
    assert [locator.url for locator in world.origin.downloads] == [url(V2), url(V1)]
    assert world.file(S1) == S1.squashfs


def test_t6_a_substitute_serve_records_the_known_good_tag(world):
    # The device's frontier (v1.1.0) is not on disk; its known-good (v1.0.0) is and is served.
    world.list(published(V1, S1), published(V2, S2), serving={url(V1): S1, url(V2): S2})
    insert_device(world.db, DEVICE_ID, serial=SERIAL, known_good_tag=V1)
    insert_device(world.db, "device-other", known_good_tag=V2)
    world.fetch(S1)
    response = world.boot()
    assert response.status_code == 200 and response.content == S1.squashfs
    assert world.reads.device(DEVICE_ID).last_served_tag == V1
    assert S2.job in world.publisher.inserted  # the wanted image's fetch was published


# -- T7: migration 028 ----------------------------------------------------------------------------


@pytest.fixture
def at_027():
    """A schema migrated through 027 with procrastinate installed, and its `Database`."""
    with schema_before("028") as db:
        yield db


def _seed_027(db: Database) -> None:
    """027's state: v1 and v2 share tarball S1, v3 ships S2; each image is keyed by its tag and
    has produced facts; a `.deb` with facts; the notes; old-shape queue rows."""
    deb_sha = "d" * 64
    with db.transaction() as conn:
        for tag, build in (("v1.0.0", S1), ("v2.0.0", S1), ("v3.0.0", S2)):
            major = int(tag[1])
            conn.execute(
                "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,base_tarball_sha256,"
                "base_tarball_size,base_tarball_url,discovered_at,updated_at) "
                "VALUES(%s,%s,0,0,FALSE,%s,%s,%s,1.0,1.0)",
                (tag, major, build.sha, len(build.tarball), url(tag)))
            conn.execute("INSERT INTO assets(kind,identity,produced_size,produced_sha256,"
                         "created_at) VALUES('os-image',%s,%s,%s,1.0)",
                         (tag, build.facts.size, build.facts.sha256))
            conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                         "locator_sha256,locator_size,added_at) "
                         "VALUES('os-image',%s,%s,%s,%s,%s,1.0)",
                         (tag, tag, url(tag), build.sha, len(build.tarball)))
        conn.execute("INSERT INTO assets(kind,identity,produced_size,produced_sha256,created_at) "
                     "VALUES('player-deb',%s,10,%s,1.0)", (deb_sha, deb_sha))
        # As 021 seeds a `.deb` reference: its locator names its key, so 028's CHECK holds.
        conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,locator_sha256,"
                     "locator_size,expected_size,expected_sha256,added_at) "
                     "VALUES('player-deb',%s,'v1.0.0','https://example.test/a.deb',%s,10,10,%s,1.0)",
                     (deb_sha, deb_sha, deb_sha))
        for lock, name in (('os_image.fetch["v1.0.0"]', "os_image.fetch"),
                           (f'player_deb.fetch["{deb_sha}"]', "player_deb.fetch")):
            conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,seq,updated_at) "
                         "VALUES(%s,%s,'ok',nextval('job_outcome_seq'),1.0)", (lock, name))
        for task, args, status in (
                ("photo_wall.os_image.fetch", {"tag": "v1.0.0", "_attempt": 0}, "todo"),
                ("photo_wall.os_image.fetch", {"tag": "v3.0.0", "_attempt": 1}, "doing"),
                ("photo_wall.player_deb.fetch", {"sha256": deb_sha}, "todo"),
                ("photo_wall.player_deb.fetch", {"sha256": "e" * 64}, "doing")):
            conn.execute("INSERT INTO procrastinate_jobs(queue_name,task_name,args,status) "
                         "VALUES('photo-wall-fetch',%s,%s,%s)", (task, json.dumps(args), status))


def _state(db: Database) -> dict:
    """What 028 decides: the os-image rows and references, the notes, the queue, the CHECK."""
    with db.transaction() as conn:
        assets = conn.execute("SELECT kind, identity, produced_sha256 FROM assets "
                              "ORDER BY kind, identity").fetchall()
        references = conn.execute("SELECT identity, owner, locator_sha256, expected_sha256 "
                                  "FROM asset_references WHERE kind='os-image' "
                                  "ORDER BY owner").fetchall()
        notes = [row["job_name"] for row in conn.execute(
            "SELECT job_name FROM job_outcomes ORDER BY job_name").fetchall()]
        queue = [(row["task_name"], row["status"]) for row in conn.execute(
            "SELECT task_name, status::text AS status FROM procrastinate_jobs ORDER BY id")]
        # The catalog is database-wide: another schema (the app's own, in CI) has its copy of
        # the constraint. `::regclass` resolves through this test's search_path, so only this
        # schema's table counts.
        check = conn.execute(
            "SELECT count(*) AS n FROM pg_constraint "
            "WHERE conname = 'asset_references_locator_names_the_key' "
            "AND conrelid = 'asset_references'::regclass").fetchone()["n"]
    return {
        "os_rows": sorted((row["identity"], row["produced_sha256"]) for row in assets
                          if row["kind"] == "os-image"),
        "deb_facts": [row["produced_sha256"] for row in assets if row["kind"] == "player-deb"],
        "references": [(row["identity"], row["owner"], row["locator_sha256"],
                        row["expected_sha256"]) for row in references],
        "notes": notes, "queue": queue, "check": check,
    }


def test_t7_028_rekeys_os_images_by_tarball_with_no_facts_and_ends_old_shape_rows(at_027):
    _seed_027(at_027)
    at_027.migrate()
    state = _state(at_027)
    assert state["os_rows"] == sorted([(S1.sha, None), (S2.sha, None)])  # keyed by sha, no facts
    assert state["references"] == [  # the locator names the key; no expected facts
        (S1.sha, "v1.0.0", S1.sha, None), (S1.sha, "v2.0.0", S1.sha, None),
        (S2.sha, "v3.0.0", S2.sha, None)]
    assert state["deb_facts"] == ["d" * 64]  # untouched
    assert state["notes"] == ["player_deb.fetch"]  # the os_image.fetch note is gone
    assert state["queue"] == [("photo_wall.os_image.fetch", "cancelled"),
                              ("photo_wall.os_image.fetch", "failed"),
                              ("photo_wall.player_deb.fetch", "todo"),
                              ("photo_wall.player_deb.fetch", "doing")]
    assert state["check"] == 1
    # The re-keyed rows are what the code reads.
    with RecordingTransactions(at_027).begin() as tx:
        asset = PgAssetRecords(ManualClock(0.0)).get(tx, asset_key(S1.job))
    assert asset is not None and asset.produced is None
    assert [ref.owner for ref in asset.references] == ["v2.0.0", "v1.0.0"]


# 028's header, verbatim: rollback steps (a) and (b), and the roll forward. Step (c) is the code
# revert and (d) the ETag reset; the old code's writes meanwhile are simulated below.
END_OS_IMAGE_DELIVERIES = (
    "UPDATE procrastinate_jobs SET status = 'cancelled' "
    "WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'todo'; "
    "UPDATE procrastinate_jobs SET status = 'failed' "
    "WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'doing';")
ROLLBACK = (END_OS_IMAGE_DELIVERIES + " ALTER TABLE asset_references DROP CONSTRAINT "
            "asset_references_locator_names_the_key; UPDATE app_release_poll SET etag = NULL;")
ROLL_FORWARD = (END_OS_IMAGE_DELIVERIES + " DELETE FROM schema_migrations "
                "WHERE name = '028_os_image_content_key.sql';")


@pytest.mark.parametrize("rolled_back", [False, True])
def test_t7b_028_runs_again_to_the_same_state(at_027, rolled_back):
    # A second run of 028 (roll forward after a rollback, or twice in a row) converges: it
    # deletes every os-image row first and replaces its CHECK.
    _seed_027(at_027)
    at_027.migrate()
    first = _state(at_027)
    if rolled_back:
        with at_027.transaction() as conn:
            conn.execute(ROLLBACK)
            # The reverted code runs: a tag-keyed OS image with facts (the CHECK is gone, or
            # this insert would be refused), its note and an old-shape delivery.
            conn.execute("INSERT INTO assets(kind,identity,produced_size,produced_sha256,"
                         "created_at) VALUES('os-image','v1.0.0',%s,%s,1.0)",
                         (S1.facts.size, S1.facts.sha256))
            conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                         "locator_sha256,locator_size,added_at) "
                         "VALUES('os-image','v1.0.0','v1.0.0',%s,%s,%s,1.0)",
                         (url("v1.0.0"), S1.sha, len(S1.tarball)))
            conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,seq,updated_at) "
                         "VALUES('os_image.fetch[\"v1.0.0\"]','os_image.fetch','ok',"
                         "nextval('job_outcome_seq'),1.0)")
            conn.execute("INSERT INTO procrastinate_jobs(queue_name,task_name,args,status) "
                         "VALUES('photo-wall-fetch','photo_wall.os_image.fetch',%s,'doing')",
                         (json.dumps({"tag": "v1.0.0", "_attempt": 0}),))
    with at_027.transaction() as conn:
        conn.execute(ROLL_FORWARD)
    at_027.migrate()
    again = _state(at_027)
    queue = again.pop("queue")
    assert queue[:len(first["queue"])] == first["queue"]
    assert queue[len(first["queue"]):] == (
        [("photo_wall.os_image.fetch", "failed")] if rolled_back else [])
    first.pop("queue")
    assert again == first  # re-keyed, no facts, no note, one CHECK
