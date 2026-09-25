"""Migration 023 carries main's served `.deb` (`app_package_policy.current_sha256`) across, and
027 records who promoted by main's rule: 'operator' where main would hold the promotion.

Each test builds a schema at main's last migration (019), seeds main's state, then runs
`Database.migrate()`, which applies the MVP's 020 onward as an upgrade does: 021 seeds the Asset
records from main's tables and 023 carries the served `.deb`. Skips without
PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from content_db import put_file
from fakes.origin import FakeReleaseOrigin
from fakes.publisher import RecordingPublisher
from psycopg.conninfo import make_conninfo

from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.content_catalog.catalog import DevicePackage, ReleaseCatalog
from central.content_catalog.ports import Promotion
from central.content_catalog.sync import SyncReleasesHandler
from central.db import Database
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
from central.infra.stored_assets import DiskStoredAssets
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.job_types import SyncReleases
from central.kernel.ports import ReleaseListing
from contracts.time import ManualClock

MIGRATIONS = Path(__file__).resolve().parents[1] / "central" / "migrations"
V1, V2, V3 = "v1.0.0", "v1.1.0", "v1.2.0"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def legacy():
    """A schema migrated to main's 019 only, and the `Database` over it (not yet upgraded)."""
    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PHOTO_WALL_TEST_DATABASE_URL for real PostgreSQL integration")
    schema = "pw_test_" + uuid.uuid4().hex
    conninfo = make_conninfo(dsn, options=f"-c search_path={schema}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    db = None
    try:
        with psycopg.connect(conninfo) as conn:
            conn.execute("""CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name >= "020":
                    break
                sql = path.read_text()
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(name,sha256) VALUES(%s,%s)",
                             (path.name, hashlib.sha256(sql.encode()).hexdigest()))
        db = Database(conninfo)
        yield db
    finally:
        if db is not None:
            db.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                psycopg.sql.Identifier(schema)))


def _main_state(db: Database, *, current: str | None, promoted: str | None,
                releases=(V1, V2, V3), cached: bool = True) -> None:
    """Main's tables: every release (mirrored into `app_packages` when `cached`), `current`
    registered and pointed at."""
    with db.transaction() as conn:
        for tag in releases:
            major, minor, patch = (int(part) for part in tag[1:].split("."))
            if cached:
                conn.execute("INSERT INTO app_packages VALUES(%s,%s,10,1.0)", (sha(tag), tag))
            conn.execute(
                "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,asset_sha256,"
                "asset_size,asset_url,mirror_state,mirrored_sha256,discovered_at,updated_at) "
                "VALUES(%s,%s,%s,%s,FALSE,%s,10,%s,%s,%s,1.0,1.0)",
                (tag, major, minor, patch, sha(tag), f"https://example.test/{tag}.deb",
                 "mirrored" if cached else "discovered", sha(tag) if cached else None))
        if current is not None:
            conn.execute("INSERT INTO app_packages VALUES(%s,'manual',10,1.0) "
                         "ON CONFLICT DO NOTHING", (current,))
            conn.execute("INSERT INTO app_package_policy VALUES(TRUE,%s)", (current,))
        if promoted is not None:
            conn.execute("INSERT INTO app_release_policy VALUES(TRUE,%s)", (promoted,))


def _pointers(db: Database) -> tuple[str | None, str | None]:
    with PgTransactions(db).begin() as tx:
        releases = PgReleaseRecords()
        return releases.promoted_tag(tx), releases.last_good_tag(tx)


def _manifest(db: Database, cache: Path, *, on_disk: tuple[str, ...]):
    """`/v1/app/manifest` with these tags' `.deb`s on disk (021 seeded their Asset records)."""
    clock = ManualClock(1000.0)
    store = CacheStore(CacheLayout(cache))
    for tag in on_disk:
        put_file(store, AssetKey(AssetKind.PLAYER_DEB, sha(tag)), b"d" * 10)
    catalog = ReleaseCatalog(
        releases=PgReleaseRecords(), devices=PgDeviceRecords(),
        stored=DiskStoredAssets(records=PgAssetRecords(clock), store=store),
        transactions=PgTransactions(db), publisher=RecordingPublisher(clock), clock=clock)
    return asyncio.run(catalog.promoted_package())


def test_a_never_promoted_served_deb_becomes_promoted_and_last_good(legacy, tmp_path):
    # P0: main served current_sha256 with nothing promoted; the MVP reads only the policy row.
    _main_state(legacy, current=sha(V2), promoted=None)
    legacy.migrate()
    assert _pointers(legacy) == (V2, V2)
    assert _manifest(legacy, tmp_path, on_disk=(V2,)) == DevicePackage(V2, V2, sha(V2), 10)


def test_a_pending_promotion_is_kept_and_the_served_deb_is_its_fallback(legacy, tmp_path):
    # Main: promoted V3 was still mirroring, current stayed on V2 until V3's bytes arrived.
    _main_state(legacy, current=sha(V2), promoted=V3)
    legacy.migrate()
    assert _pointers(legacy) == (V3, V2)
    assert _manifest(legacy, tmp_path, on_disk=(V2,)) == DevicePackage(V2, V2, sha(V2), 10)
    assert _manifest(legacy, tmp_path, on_disk=(V2, V3)) == DevicePackage(V3, V3, sha(V3), 10)


def test_a_converged_promotion_is_its_own_last_good(legacy):
    _main_state(legacy, current=sha(V1), promoted=V1)
    legacy.migrate()
    assert _pointers(legacy) == (V1, V1)


def test_a_served_deb_matching_no_release_writes_nothing(legacy):
    # A manually uploaded .deb: the MVP cannot serve it (every .deb comes from GitHub). Nothing
    # is invented; the migration WARNs and the sync tail logs why the manifest is 503.
    _main_state(legacy, current=sha("manual upload"), promoted=None)
    legacy.migrate()
    assert _pointers(legacy) == (None, None)


def test_a_fresh_install_writes_nothing(legacy):
    _main_state(legacy, current=None, promoted=None, releases=())
    legacy.migrate()
    assert _pointers(legacy) == (None, None)


def _promotion(db: Database) -> Promotion | None:
    with PgTransactions(db).begin() as tx:
        return PgReleaseRecords().promotion(tx)


def _sync(db: Database, cache: Path) -> None:
    """One periodic release sync over the upgraded schema: no bound players, nothing new."""
    clock = ManualClock(1000.0)
    assets = PgAssetRecords(clock)
    transactions = PgTransactions(db)
    publisher = RecordingPublisher(clock)
    catalog = ReleaseCatalog(
        releases=PgReleaseRecords(), devices=PgDeviceRecords(),
        stored=DiskStoredAssets(records=assets, store=CacheStore(CacheLayout(cache))),
        transactions=transactions, publisher=publisher, clock=clock)
    handler = SyncReleasesHandler(
        origin=FakeReleaseOrigin(ReleaseListing((), None, unchanged=True), {}),
        releases=PgReleaseRecords(), devices=PgDeviceRecords(), assets=assets,
        transactions=transactions, publisher=publisher, catalog=catalog, clock=clock)
    asyncio.run(handler.handle(SyncReleases()))


def _bind_a_player(db: Database) -> None:
    """One binding in main's 001 tables: main's `bound_player_count` becomes 1."""
    with db.transaction() as conn:
        conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,"
                     "device_id) VALUES('p1','pk1','th1',1.0,1.0,'d1')")
        conn.execute("INSERT INTO outputs VALUES('p1','HDMI-A-1','{}')")
        conn.execute("INSERT INTO frames(id,surface_id,x_mm,y_mm,width_mm,height_mm,profile,"
                     "calibration) VALUES('f1','s1',0,0,100,100,'{}','{}')")
        conn.execute("INSERT INTO bindings VALUES('f1','p1','HDMI-A-1')")


# 027 classifies an existing promotion by main's rule (`_autopull_deb`, then the MVP's sync):
# main held it iff a player is bound AND a `.deb` is cached, and moved it to the newest (V3)
# otherwise. `current` is main's served `.deb`; each release's `.deb` is in `app_packages`
# (cached) unless `cached` is False.
@pytest.mark.parametrize("promoted,current,bound,cached,by,after_sync", [
    # Not held by main: 'auto', and the sync follows the newest as main would.
    (V1, sha(V1), False, True, "auto", V3),  # main's promotion, no bound player
    (None, sha(V1), False, True, "auto", V3),  # 023's carry, no bound player
    (V1, None, True, False, "auto", V3),  # bound, but no `.deb` cached
    # Held by main: 'operator', and the sync never moves it.
    (V1, sha(V1), True, True, "operator", V1),
    (None, sha(V1), True, True, "operator", V1),  # 023's carry
])
def test_an_upgraded_promotion_is_classified_by_mains_rule(
        legacy, tmp_path, promoted, current, bound, cached, by, after_sync):
    _main_state(legacy, current=current, promoted=promoted, cached=cached)
    if bound:
        _bind_a_player(legacy)
    legacy.migrate()
    assert _promotion(legacy) == Promotion(V1, by)
    _sync(legacy, tmp_path)
    assert _promotion(legacy) == Promotion(after_sync, by)
