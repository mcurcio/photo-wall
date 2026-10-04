"""`MaintainCacheHandler`: every cached release file nothing wants is removed, nothing wanted ever is
(auto-ingest design §6.3). Real sync, records and selection over PostgreSQL; the cache is the
world's `tmp_path` directory, its mtimes set relative to the world's manual clock."""

from __future__ import annotations

import asyncio
import os
import threading
from uuid import UUID

import pytest
from test_node_release_catalog import store_files
from test_node_release_ingest import Upstream, make_sync_world, node_upload, sync

from central.assets.layout import TEMP_PREFIX
from central.assets.maintenance import MAX_UNLINKS_PER_RUN, MaintainCacheHandler
from central.fleet.node_boot import NodeBootService
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.infra.retention import PgCacheRetention
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.handling import handler_job_type
from central.kernel.job_types import MaintainCache
from central.kernel.jobs import asset_key
from contracts.node_boot import NodeBootRequestV2

HOUR = 3600.0
OLD = 2 * HOUR  # past every grace
TAGS = ("v1.8.0", "v1.9.0", "v2.0.0", "v2.1.0", "v2.2.0", "v2.3.0", "v2.4.0")


def age(path, world, seconds) -> None:
    at = world.clock.utc() - seconds
    os.utime(path, (at, at))


def cleaner(world, registry) -> MaintainCacheHandler:
    return MaintainCacheHandler(retention=PgCacheRetention(world.catalog), records=world.assets,
                                store=world.store, transactions=PgTransactions(registry.db),
                                clock=world.clock)


def files(world, upload) -> set:
    with world.db.transaction() as conn:
        from central.infra.node_releases import deployment_jobs
        jobs = deployment_jobs(conn, [upload.deployment_id])[upload.deployment_id]
    return {world.store.layout.path(asset_key(job)) for job in jobs}


@pytest.fixture
def wall(registry, tmp_path):
    """Seven stable releases, every file cached and old. Wanted: v1.8.0 only by a live boot
    offer, v2.0.0 as the previous selection, v2.1.0 selected, v2.2.0-v2.4.0 the window.
    v1.9.0 is wanted by nothing."""
    upstream = Upstream()
    uploads = {tag: node_upload(tag) for tag in TAGS}
    for upload in uploads.values():
        upstream.put(upload)
    world = make_sync_world(registry, tmp_path)(upstream)
    sync(world)
    sessions = NodeSessions(registry.db, world.clock, NodeControlConfig("node-test"))
    boots = NodeBootService(sessions)
    boots.select(uploads["v1.8.0"].deployment_id, 0)
    boots.offer(NodeBootRequestV2("abcdef1234567890", UUID(int=9), "c" * 64))  # a Pi mid-prepare
    boots.select(uploads["v2.0.0"].deployment_id, 1)
    boots.select(uploads["v2.1.0"].deployment_id, 2)
    for upload in uploads.values():
        store_files(world, upload)
        for path in files(world, upload):
            age(path, world, OLD)
    return world, uploads, boots


def test_handler_serves_maintain_cache(registry, tmp_path):
    world = make_sync_world(registry, tmp_path)(Upstream())
    assert handler_job_type(cleaner(world, registry)) is MaintainCache


def test_every_desired_file_is_kept_and_only_the_unwanted_release_is_removed(registry, wall):
    world, uploads, _ = wall
    removed = set(cleaner(world, registry).sweep())
    assert removed == files(world, uploads["v1.9.0"])
    for tag in TAGS:
        if tag != "v1.9.0":
            assert all(path.exists() for path in files(world, uploads[tag])), tag
    # The live offer (< 1 h) was v1.8.0's only root: past its lifetime, its files go too.
    world.clock.advance(HOUR + 1)
    assert set(cleaner(world, registry).sweep()) == files(world, uploads["v1.8.0"])
    # A removed file is not a lost asset: its record and facts stay for a re-fetch.
    key = AssetKey(AssetKind.OS_IMAGE, uploads["v1.9.0"].release.base.content_key)
    assert world.reads.asset(key).produced is not None


def test_a_v1_desired_deb_is_kept(registry, tmp_path):
    from content_db import facts_of, put_file
    from test_content_catalog_sync import deb, published
    world = make_sync_world(registry, tmp_path)(Upstream(), releases=[published("v0.0.1")],
                                                promoted="v0.0.1")
    kept = AssetKey(AssetKind.PLAYER_DEB, deb("v0.0.1").sha256)
    gone = AssetKey(AssetKind.PLAYER_DEB, "9" * 64)
    for key in (kept, gone):
        age(put_file(world.store, key, b"deb bytes"), world, OLD)
    assert cleaner(world, registry).sweep() == [world.store.layout.path(gone)]
    assert world.store.present(kept, facts_of(b"deb bytes"))


def test_grace_spares_a_fresh_file_and_a_recently_served_one(registry, wall):
    world, uploads, _ = wall
    fresh, served = files(world, uploads["v1.9.0"]), uploads["v1.9.0"].release.base.content_key
    for path in fresh:
        age(path, world, HOUR - 60)  # a fetch that landed after the desired read
    assert cleaner(world, registry).sweep() == []
    for path in fresh:
        age(path, world, OLD)
    key = AssetKey(AssetKind.OS_IMAGE, served)
    with world.reads.transactions.begin() as tx:
        world.assets.touch_served(tx, key, world.clock.utc() - (HOUR - 60))  # a live stream
    removed = cleaner(world, registry).sweep()
    assert world.store.layout.path(key) not in removed and len(removed) == len(fresh) - 1


def test_temp_files_go_only_when_idle_and_previews_and_media_are_never_touched(registry, wall):
    world, _, _ = wall
    apps = world.store.layout.directory(AssetKind.SEALED_ENVIRONMENT)
    idle, live = apps / f"{TEMP_PREFIX}idle", apps / f"{TEMP_PREFIX}live"
    previews = world.store.layout.directory(AssetKind.LIBRARY_THUMBNAIL)
    previews.mkdir(parents=True, exist_ok=True)
    media = world.store.layout.directory(AssetKind.OS_IMAGE).parent / "media"
    media.mkdir(exist_ok=True)
    untouchable = [previews / ("asset-" + "1" * 64 + ".jpg"), media / "clip.mp4",
                   apps / "notes.txt"]
    for path in (idle, live, *untouchable):
        path.write_bytes(b"x")
        age(path, world, OLD)
    age(live, world, 60)  # a download still writing
    removed = cleaner(world, registry).sweep()
    assert idle in removed and live not in removed
    assert all(path.exists() for path in untouchable)


def test_at_most_fifty_files_per_run(registry, tmp_path):
    world = make_sync_world(registry, tmp_path)(Upstream())
    apps = world.store.layout.directory(AssetKind.SEALED_ENVIRONMENT)
    apps.mkdir(parents=True)
    for index in range(MAX_UNLINKS_PER_RUN + 10):
        path = apps / f"environment-{index:064x}.tar"
        path.write_bytes(b"x")
        age(path, world, OLD)
    assert len(cleaner(world, registry).sweep()) == MAX_UNLINKS_PER_RUN
    assert len(os.listdir(apps)) == 10
    asyncio.run(cleaner(world, registry).handle(MaintainCache()))  # the next run takes the rest
    assert os.listdir(apps) == []


def test_a_select_racing_the_sweep_waits_for_it(registry, wall):
    """The cleaner holds the asset-roots lock from its desired read through its deletes; a
    selection made meanwhile commits only after them (and then re-fetches by read-through)."""
    world, uploads, boots = wall
    retention = PgCacheRetention(world.catalog)
    held, go = threading.Event(), threading.Event()

    class Pausing:
        def hold_desired(self, tx):
            desired = retention.hold_desired(tx)
            held.set()
            assert go.wait(10)
            return desired

    handler = MaintainCacheHandler(retention=Pausing(), records=world.assets, store=world.store,
                                   transactions=PgTransactions(registry.db), clock=world.clock)
    removed: list = []
    sweeper = threading.Thread(target=lambda: removed.extend(handler.sweep()))
    sweeper.start()
    assert held.wait(10)
    selected = threading.Event()
    selector = threading.Thread(target=lambda: (
        boots.select(uploads["v1.9.0"].deployment_id, 3), selected.set()))
    selector.start()
    assert not selected.wait(0.5)  # blocked on the lock the sweep holds
    go.set()
    sweeper.join(10)
    selector.join(10)
    assert selected.is_set() and set(removed) == files(world, uploads["v1.9.0"])
    # From now on the selection is desired: the next sweep keeps (and removes nothing of) it.
    store_files(world, uploads["v1.9.0"])
    for path in files(world, uploads["v1.9.0"]):
        age(path, world, OLD)
    assert not set(cleaner(world, registry).sweep()) & files(world, uploads["v1.9.0"])
