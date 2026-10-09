"""`MaintainCacheHandler`: every cached release file nothing wants is removed, nothing wanted ever is
(auto-ingest design §6.3). Real sync, records and selection over PostgreSQL; the cache is the
world's `tmp_path` directory. File ages are set against the real filesystem clock (the cleaner
measures them against its marker file), never against the world's manual clock."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from test_node_release_catalog import store_files
from test_node_release_ingest import Upstream, make_sync_world, node_upload, sync

from central.assets import maintenance
from central.assets.layout import TEMP_PREFIX
from central.assets.maintenance import CLOCK_MARKER, MAX_UNLINKS_PER_RUN, MaintainCacheHandler
from central.fleet.node_boot import NodeBootService
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.handling import handler_job_type
from central.kernel.job_types import MaintainCache
from central.kernel.jobs import asset_key

HOUR = 3600.0
OLD = 2 * HOUR  # past every grace
TAGS = ("v1.8.0", "v1.9.0", "v2.0.0", "v2.1.0", "v2.2.0", "v2.3.0", "v2.4.0")


def age(path, seconds, *, now=None) -> None:
    at = (time.time() if now is None else now) - seconds
    os.utime(path, (at, at))


def cleaner(world, registry, catalog=None) -> MaintainCacheHandler:
    return MaintainCacheHandler(catalog=catalog or world.catalog, records=world.assets,
                                store=world.store, transactions=PgTransactions(registry.db))


def sweep(handler) -> list[Path]:
    return asyncio.run(handler.sweep())


def files(world, upload) -> set:
    with world.db.transaction() as conn:
        from central.infra.node_releases import deployment_jobs
        jobs = deployment_jobs(conn, [upload.deployment_id])[upload.deployment_id]
    return {world.store.layout.path(asset_key(job)) for job in jobs}


@pytest.fixture
def wall(registry, tmp_path):
    """Seven stable releases, every file cached and old. Wanted: v2.0.0 as the previous
    selection, v2.1.0 selected, v2.2.0-v2.4.0 the window. v1.8.0 (selected twice before) and
    v1.9.0 are wanted by nothing."""
    upstream = Upstream()
    uploads = {tag: node_upload(tag) for tag in TAGS}
    for upload in uploads.values():
        upstream.put(upload)
    world = make_sync_world(registry, tmp_path)(upstream)
    sync(world)
    boots = NodeBootService(NodeSessions(registry.db, world.clock, NodeControlConfig("node-test")))
    boots.select(uploads["v1.8.0"].deployment_id, 0)
    boots.select(uploads["v2.0.0"].deployment_id, 1)
    boots.select(uploads["v2.1.0"].deployment_id, 2)
    for upload in uploads.values():
        store_files(world, upload)
        for path in files(world, upload):
            age(path, OLD)
    return world, uploads, boots


def test_handler_serves_maintain_cache(registry, tmp_path):
    world = make_sync_world(registry, tmp_path)(Upstream())
    assert handler_job_type(cleaner(world, registry)) is MaintainCache


def test_every_desired_file_is_kept_and_only_the_unwanted_releases_are_removed(registry, wall):
    world, uploads, _ = wall
    removed = set(sweep(cleaner(world, registry)))
    assert removed == files(world, uploads["v1.8.0"]) | files(world, uploads["v1.9.0"])
    for tag in TAGS[2:]:
        assert all(path.exists() for path in files(world, uploads[tag])), tag
    # A removed file is not a lost asset: its record and facts stay for a re-fetch.
    key = AssetKey(AssetKind.OS_IMAGE, uploads["v1.9.0"].release.base.content_key)
    assert world.reads.asset(key).produced is not None


def test_the_promoted_deb_is_kept(registry, tmp_path):
    from content_db import facts_of, put_file
    from test_content_catalog_sync import deb

    from central.content_catalog.ports import ReleaseRow
    world = make_sync_world(registry, tmp_path)(
        Upstream(), releases=[ReleaseRow("v0.0.1", False, deb("v0.0.1"), None)],
        promoted="v0.0.1")
    kept = AssetKey(AssetKind.PLAYER_DEB, deb("v0.0.1").sha256)
    gone = AssetKey(AssetKind.PLAYER_DEB, "9" * 64)
    for key in (kept, gone):
        age(put_file(world.store, key, b"deb bytes"), OLD)
    assert sweep(cleaner(world, registry)) == [world.store.layout.path(gone)]
    assert world.store.present(kept, facts_of(b"deb bytes"))


def test_a_key_that_becomes_desired_between_mark_and_unlink_is_kept(registry, wall):
    """Mark and sweep without a lock: the desired set is re-read right before each unlink, so a
    selection committed after the run picked its candidates still keeps its files."""
    world, uploads, boots = wall

    class SelectingMidRun:
        """The catalog; its second read (the first re-check) follows a selection of v1.9.0."""

        def __init__(self) -> None:
            self.reads = 0

        async def desired_assets(self):
            self.reads += 1
            if self.reads == 2:
                boots.select(uploads["v1.9.0"].deployment_id, 3)
            return await world.catalog.desired_assets()

    removed = set(sweep(cleaner(world, registry, SelectingMidRun())))
    assert removed == files(world, uploads["v1.8.0"])
    assert all(path.exists() for path in files(world, uploads["v1.9.0"]))


def test_grace_spares_a_fresh_file_and_a_recently_served_one(registry, wall):
    world, uploads, _ = wall
    unwanted = files(world, uploads["v1.8.0"]) | files(world, uploads["v1.9.0"])
    for path in unwanted:
        age(path, HOUR - 60)  # fetches that landed after the desired read
    assert sweep(cleaner(world, registry)) == []
    for path in unwanted:
        age(path, OLD)
    key = AssetKey(AssetKind.OS_IMAGE, uploads["v1.9.0"].release.base.content_key)
    with world.reads.transactions.begin() as tx:
        world.assets.touch_served(tx, key)  # a live stream, on the database's clock
    removed = sweep(cleaner(world, registry))
    assert world.store.layout.path(key) not in removed and len(removed) == len(unwanted) - 1


def test_ages_are_measured_on_the_cache_filesystem_clock_never_this_workers(
        registry, wall, monkeypatch):
    """The cache filesystem's clock runs a day behind this worker's: a file written 10 minutes
    ago by the filesystem's clock (a day and 10 minutes by the worker's) is still in its grace,
    and one written 2 hours ago by the filesystem's clock is past it."""
    world, uploads, _ = wall
    behind = time.time() - 24 * HOUR
    real_touch = Path.touch

    def filesystem_a_day_behind(path, *args, **kwargs):
        real_touch(path, *args, **kwargs)
        if path.name == CLOCK_MARKER:
            os.utime(path, (behind, behind))

    monkeypatch.setattr(Path, "touch", filesystem_a_day_behind)
    fresh, old = files(world, uploads["v1.8.0"]), files(world, uploads["v1.9.0"])
    for path in fresh:
        age(path, 600, now=behind)
    for path in old:
        age(path, OLD, now=behind)
    assert set(sweep(cleaner(world, registry))) == old
    marker = world.store.layout.root / CLOCK_MARKER
    assert marker.exists() and os.stat(marker).st_mtime == behind


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
        age(path, OLD)
    age(live, 60)  # a download still writing
    removed = sweep(cleaner(world, registry))
    assert idle in removed and live not in removed
    assert all(path.exists() for path in untouchable)


def test_at_most_fifty_files_per_run(registry, tmp_path):
    world = make_sync_world(registry, tmp_path)(Upstream())
    apps = world.store.layout.directory(AssetKind.SEALED_ENVIRONMENT)
    apps.mkdir(parents=True)
    for index in range(MAX_UNLINKS_PER_RUN + 10):
        path = apps / f"environment-{index:064x}.tar"
        path.write_bytes(b"x")
        age(path, OLD)
    assert len(sweep(cleaner(world, registry))) == MAX_UNLINKS_PER_RUN
    assert len(os.listdir(apps)) == 10
    asyncio.run(cleaner(world, registry).handle(MaintainCache()))  # the next run takes the rest
    assert os.listdir(apps) == []


def test_a_file_rewritten_after_it_was_picked_is_kept(registry, wall, monkeypatch):
    """A fetch that installs a key's file after the run picked it (a fresh mtime) is spared."""
    world, uploads, _ = wall
    picked = maintenance.MaintainCacheHandler._candidates

    def then_rewritten(self, desired):
        found = picked(self, desired)
        for candidate in found:
            age(candidate.path, 0)
        return found

    monkeypatch.setattr(maintenance.MaintainCacheHandler, "_candidates", then_rewritten)
    assert sweep(cleaner(world, registry)) == []
    assert all(path.exists() for path in files(world, uploads["v1.9.0"]))
