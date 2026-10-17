"""The node release read (console DDD Part E G1, R18; auto-ingest design §6.5; a version per Pi,
Q2 A, 2026-10-16).

Releases are ingested by the real sync from a mock upstream (`test_node_release_ingest`); the read
is `NodeReleaseCatalog.view` over the same database, with the one readiness check over the
world's cache directory.
"""
import pytest
from content_db import facts_of, put_file
from test_node_boot import NOTHING_READY, enroll, player_versions
from test_node_release_ingest import Upstream, make_sync_world, node_upload, read, sync

from central.db import Database
from central.fleet.node_release_catalog import NodeReleaseCatalog
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.infra.node_releases import deployment_jobs
from central.kernel.jobs import asset_key


def publication():
    """(origin, release, files) of one valid release with its app, on a mock upstream (the
    browser tests' fixture)."""
    upstream, upload = Upstream(), node_upload("v9.0.0")
    upstream.put(upload)
    return upstream.origin(), upload.release, upstream.files


def discover(registry, origin):
    """What one release sync's node half does for `origin`'s first release: observe and ingest."""
    import asyncio

    from central.infra.node_releases import PgNodeReleaseRecords
    from central.infra.transactions import PgTransaction
    release = asyncio.run(origin.list_releases(etag=None)).releases[0]
    with registry.db.transaction() as conn:
        PgNodeReleaseRecords(registry.clock).ingest(PgTransaction(conn), release,
                                                    now=registry.clock.utc())
    return release


@pytest.fixture
def sync_world(registry, tmp_path):
    return make_sync_world(registry, tmp_path)


class _Recording:
    """The registry's Database, recording every statement each transaction runs, in order."""

    def __init__(self, db):
        self.db, self.statements = db, []

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def recorded():
            with self.db.transaction() as conn:
                outer = self

                class Conn:
                    def execute(self, sql, *args):
                        outer.statements.append(sql)
                        return conn.execute(sql, *args)
                yield Conn()
        return recorded()

    # The real read snapshot, over the recording transaction.
    read_snapshot = Database.read_snapshot


def store_files(world, upload, *, missing=()):
    """Put `upload`'s deployment files in the world's cache (produced facts plus file)."""
    with world.db.transaction() as conn:
        jobs = deployment_jobs(conn, [upload.deployment_id])[upload.deployment_id]
    for job in jobs:
        key = asset_key(job)
        if key.identity in missing:
            continue
        data = ("bytes of " + key.identity).encode()
        with world.reads.transactions.begin() as tx:
            world.assets.record_produced(tx, key, facts_of(data))
        put_file(world.store, key, data)
    return jobs


def test_release_read_is_one_repeatable_read_read_only_snapshot(registry, sync_world):
    upstream = Upstream()
    upstream.put(node_upload("v2.0.0"))
    sync(sync_world(upstream))
    recording = _Recording(registry.db)
    sessions = NodeSessions(recording, registry.clock, NodeControlConfig("node-test"))
    result = NodeReleaseCatalog(sessions, readiness=NOTHING_READY).view()
    # One transaction: the snapshot is fixed before its first data query, and it writes nothing.
    assert recording.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in recording.statements[1:])
    assert result.read_at == registry.clock.utc()


def test_with_nothing_observed_nothing_is_listed_and_no_release_is_the_default(registry):
    result = read(registry)
    assert (result["releases"], result["players"], result["default"]) == ([], [], None)


def test_rows_are_semver_ordered_with_their_own_deployment_and_window(registry, sync_world):
    upstream = Upstream()
    uploads = [node_upload(tag) for tag in ("v2.0.0", "v2.10.0", "v2.2.0", "v2.3.0")]
    for upload in uploads:
        upstream.put(upload)
    rc = node_upload("v2.11.0-rc.1")
    upstream.put(rc, prerelease=True)
    sync(sync_world(upstream))
    rows = read(registry)["releases"]
    assert [row["version"] for row in rows] == ["v2.11.0-rc.1", "v2.10.0", "v2.3.0", "v2.2.0", "v2.0.0"]
    by_tag = {row["version"]: row for row in rows}
    # The window is the newest 3 STABLE releases: never a pre-release.
    assert [row["version"] for row in rows if row["in_window"]] == ["v2.10.0", "v2.3.0", "v2.2.0"]
    assert by_tag["v2.11.0-rc.1"]["stable"] is False and by_tag["v2.0.0"]["stable"] is True
    for upload in uploads:
        row = by_tag[upload.tag]
        assert row["base_tag"] == upload.tag
        assert row["app_environment_sha256"] == upload.release.app_environment.environment_sha256


def test_readiness_is_derived_from_the_cache_for_each_row(registry, sync_world):
    upstream = Upstream()
    old, ready, partial = node_upload("v2.0.0"), node_upload("v2.1.0"), node_upload("v2.2.0")
    for upload in (old, ready, partial):
        upstream.put(upload)
    extra = [node_upload(tag) for tag in ("v2.3.0", "v2.4.0")]  # push v2.0.0 out of the window
    for upload in extra:
        upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    store_files(world, ready)
    store_files(world, partial, missing={partial.release.manager_primary.environment_sha256})
    by_tag = {row["version"]: row for row in read(registry, world)["releases"]}
    assert (by_tag["v2.1.0"]["readiness"], by_tag["v2.1.0"]["missing_bytes"]) == ("ready", 0)
    # Wanted (in the window) with a file absent: downloading, with the absent file's size left.
    assert by_tag["v2.2.0"]["readiness"] == "downloading"
    assert by_tag["v2.2.0"]["missing_bytes"] == partial.release.manager_primary.size_bytes
    # Not wanted (out of the window, never selected): not downloaded.
    assert by_tag["v2.0.0"]["in_window"] is False
    assert by_tag["v2.0.0"]["readiness"] == "not_downloaded"
    assert by_tag["v2.0.0"]["missing_bytes"] > 0


def test_a_terminal_fetch_or_a_full_disk_reads_failed_with_its_reason(registry, sync_world):
    from central.infra.outcomes import JobOutcomes
    upstream, upload = Upstream(), node_upload("v2.0.0")
    upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    jobs = store_files(world, upload, missing={upload.release.base.content_key})
    base = next(job for job in jobs if asset_key(job).identity == upload.release.base.content_key)
    with world.reads.transactions.begin() as tx:
        JobOutcomes().record(tx, base, status="transient", reason="cache_disk_full",
                             retry_not_before=world.clock.utc() + 60, now=world.clock.utc())
    [row] = read(registry, world)["releases"]
    assert (row["readiness"], row["readiness_reason"]) == ("failed", "cache_disk_full")
    with world.reads.transactions.begin() as tx:
        JobOutcomes().record(tx, base, status="terminal", reason="all_references_rejected",
                             retry_not_before=None, now=world.clock.utc())
    [row] = read(registry, world)["releases"]
    assert (row["readiness"], row["readiness_reason"]) == ("failed", "all_references_rejected")
    store_files(world, upload)  # data first: the file decides over any outcome
    [row] = read(registry, world)["releases"]
    assert (row["readiness"], row["readiness_reason"]) == ("ready", None)


def test_each_pi_reads_its_own_version_and_its_previous_one(registry, sync_world):
    """Install on one Pi: its version, chosen, with the one before as its previous; another Pi with
    no version reads the default (the newest ready stable release), not chosen."""
    upstream, first, second = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    upstream.put(first)
    upstream.put(second)
    world = sync_world(upstream)
    sync(world)
    store_files(world, first)
    for serial in ("pi-a", "pi-b"):
        enroll(registry.db, serial, registry.clock.utc())
    versions = player_versions(NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test")),
                               readiness=world.stored)
    versions.install("pi-a", "v2.0.0")
    versions.install("pi-a", "v2.1.0")
    players = {pi["serial"]: pi for pi in read(registry, world)["players"]}
    assert {key: players["pi-a"][key] for key in ("version", "chosen", "previous", "install")} == {
        "version": "v2.1.0", "chosen": True, "previous": "v2.0.0", "install": "downloading"}
    assert {key: players["pi-b"][key] for key in ("version", "chosen", "previous", "install")} == {
        "version": "v2.0.0", "chosen": False, "previous": None, "install": None}
    assert read(registry, world)["default"] == "v2.0.0"


def test_actual_producer_manifest_roundtrips_through_the_sync(registry, sync_world, tmp_path):
    import httpx
    from test_node_release_artifacts import inputs

    from central.origins.github import GitHubReleaseOrigin
    from contracts.node_release import NODE_RELEASE_MANIFEST, parse_node_release
    _, _, output = inputs(tmp_path)
    raw = (output / NODE_RELEASE_MANIFEST).read_bytes()
    manifest = parse_node_release(raw)
    entry = {"tag_name": manifest.base.tag, "draft": False, "prerelease": False, "assets": [
        {"name": path.name, "browser_download_url": "https://assets.test/" + path.name, "id": index + 1,
         "updated_at": "2026-09-30T00:00:00Z"} for index, path in enumerate(output.iterdir())]}

    def handle(request):
        if request.url.path == "/repos/test/repo/releases":
            return httpx.Response(200, json=[entry])
        path = output / request.url.path.lstrip("/")
        return httpx.Response(200, content=path.read_bytes()) if path.is_file() else httpx.Response(404)

    upstream = Upstream()
    world = sync_world(upstream)
    world.handler._origin = GitHubReleaseOrigin("test/repo", transport=httpx.MockTransport(handle))
    sync(world)
    [row] = read(registry)["releases"]
    assert row["version"] == manifest.base.tag and row["problem"] is None
    assert row["base_tag"] == manifest.base.tag
