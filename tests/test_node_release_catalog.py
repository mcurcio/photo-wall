"""The node release read (console DDD Part E G1, R18; auto-ingest design §6.5).

Releases are ingested by the real sync from a mock upstream (`test_node_release_ingest`); the read
is `NodeReleaseCatalog.list` over the same database, with the one readiness check over the
world's cache directory.
"""
from dataclasses import replace
from uuid import uuid4

import pytest
from content_db import facts_of, put_file
from test_node_release_ingest import Upstream, make_sync_world, node_upload, read, sync

from central.fleet.node_boot import NodeBootService
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


def _boots(registry):
    return NodeBootService(NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test")))


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
    result = NodeReleaseCatalog(sessions).list()
    # One transaction: the snapshot is fixed before its first data query, and it writes nothing.
    assert recording.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in recording.statements[1:])
    assert result["read_at"] == registry.clock.utc()


def test_with_no_policy_row_the_selection_is_revision_zero_and_nothing_is_listed(registry):
    result = read(registry)
    assert result["selection"] == {"revision": 0, "deployment_id": None,
                                   "previous_deployment_id": None, "changed_at": None,
                                   "auto": None}
    assert result["deployments"] == [] and result["releases"] == []


def test_rows_are_semver_ordered_with_their_own_deployment_and_window(registry, sync_world):
    upstream = Upstream()
    uploads = [node_upload(tag) for tag in ("v2.0.0", "v2.10.0", "v2.2.0", "v2.3.0")]
    for upload in uploads:
        upstream.put(upload)
    rc = node_upload("v2.11.0-rc.1")
    upstream.put(rc, prerelease=True)
    sync(sync_world(upstream))
    rows = read(registry)["releases"]
    assert [row["tag"] for row in rows] == ["v2.11.0-rc.1", "v2.10.0", "v2.3.0", "v2.2.0", "v2.0.0"]
    by_tag = {row["tag"]: row for row in rows}
    # The window is the newest 3 STABLE releases: never a pre-release.
    assert [row["tag"] for row in rows if row["in_window"]] == ["v2.10.0", "v2.3.0", "v2.2.0"]
    assert by_tag["v2.11.0-rc.1"]["stable"] is False and by_tag["v2.0.0"]["stable"] is True
    for upload in uploads:
        row = by_tag[upload.tag]
        assert row["deployment_id"] == str(upload.deployment_id)
        assert row["base_tag"] == upload.tag and row["manifest_sha256"] == upload.sha
        assert row["app_environment_sha256"] == upload.release.app_environment.environment_sha256
    assert "verified_at" not in rows[0]


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
    by_tag = {row["tag"]: row for row in read(registry, world)["releases"]}
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


def test_the_selection_names_its_previous_deployment(registry, sync_world):
    upstream, first, second = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    upstream.put(first)
    upstream.put(second)
    sync(sync_world(upstream))
    boots = _boots(registry)
    boots.select(first.deployment_id, 0)
    boots.select(second.deployment_id, 1)
    selection = read(registry)["selection"]
    assert selection["deployment_id"] == str(second.deployment_id)
    assert selection["previous_deployment_id"] == str(first.deployment_id)
    assert selection["auto"] is None  # a selection exists: Central never selects by itself


def test_the_selected_and_previous_deployments_are_listed_even_when_older_than_the_newest_fifty(
        registry):
    from test_node_boot import cold_setup

    from central.fleet.node_boot import encode_node_deployment
    _, _, selected = cold_setup(registry)
    changed_at = registry.clock.utc()
    older = replace(selected, deployment_id=uuid4())
    newer = [replace(selected, deployment_id=uuid4()) for _ in range(50)]
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_deployments VALUES(%s,%s,%s)",
                     (older.deployment_id, encode_node_deployment(older), changed_at - 10))
        for offset, deployment in enumerate(newer, start=1):
            conn.execute("INSERT INTO node_deployments VALUES(%s,%s,%s)",
                         (deployment.deployment_id, encode_node_deployment(deployment), changed_at + offset))
    result = read(registry)
    listed = [row["deployment_id"] for row in result["deployments"]]
    # The 50 newest, newest first, then the selected one; the older unselected one is not listed.
    assert listed == [str(d.deployment_id) for d in reversed(newer)] + [str(selected.deployment_id)]
    assert str(older.deployment_id) not in listed
    assert result["selection"] == {"revision": 1, "deployment_id": str(selected.deployment_id),
                                   "previous_deployment_id": None, "changed_at": changed_at,
                                   "auto": None}
    assert result["deployments"][-1] == {
        "deployment_id": str(selected.deployment_id), "published_at": changed_at,
        "base_tag": selected.base.tag,
        "app_environment_sha256": selected.app_environment.environment_sha256}


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
    assert row["tag"] == manifest.base.tag and row["problem"] is None
    assert row["deployment_id"] is not None
