"""Node releases arrive by themselves (auto-ingest design §6.1, §6.4, §12 tracer).

The origin is the real `GitHubReleaseOrigin` over an `httpx.MockTransport` upstream; the sync,
the records and the deployment writer are the real ones (PostgreSQL, the `registry` fixture).
The origin-only tests at the top need no database.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from test_fleet_attempts import BASE_ABI
from test_node_boot import environment

from central.content_catalog.deployment import deployment_for_release, deployment_id_for
from central.kernel.handling import OriginUnavailable
from central.origins.github import GitHubReleaseOrigin
from contracts.node_boot import NodeBaseRefV2
from contracts.node_release import (
    NODE_RELEASE_MANIFEST,
    NodeReleaseAssetV2,
    NodeReleaseV2,
    encode_node_release,
)

REPO = "test/repo"
_ROLES = ("base", "boot", "node-base-deb", "node-display-deb", "manager-primary-deb",
          "manager-primary", "build-provenance", "app", "app-deb")
_IDS = itertools.count(1)


@dataclass(frozen=True)
class NodeUpload:
    """One node release's uploaded bytes: its manifest and every declared artifact."""

    tag: str
    release: NodeReleaseV2
    manifest: bytes
    files: dict[str, bytes]

    @property
    def sha(self) -> str:
        return sha256(self.manifest).hexdigest()

    @property
    def deployment_id(self) -> UUID:
        return deployment_id_for(self.sha, self.release.app_environment is not None)


def node_upload(tag: str, *, revision: str = "a" * 40, salt: str = "", app: bool = True,
                size: int = 0) -> NodeUpload:
    """A valid node release for `tag`; `salt` re-cuts every artifact (new digests)."""
    roles = _ROLES if app else _ROLES[:-2]
    bodies = {role: f"{tag}{salt}:{role}".encode() + b"x" * size for role in roles}
    artifacts = tuple(NodeReleaseAssetV2(role, f"{role}.bin", sha256(data).hexdigest(), len(data))
                      for role, data in bodies.items())
    refs = {asset.role: asset for asset in artifacts}
    manager = replace(environment("2", "photo-wall-node-manager"),
                      environment_sha256=refs["manager-primary"].sha256,
                      size_bytes=refs["manager-primary"].size_bytes,
                      deb_sha256=refs["manager-primary-deb"].sha256)
    app_ref = replace(environment(), environment_sha256=refs["app"].sha256,
                      size_bytes=refs["app"].size_bytes,
                      deb_sha256=refs["app-deb"].sha256) if app else None
    base = NodeBaseRefV2(tag, refs["base"].sha256, sha256(f"{tag}{salt}:squashfs".encode()).hexdigest(),
                         100, BASE_ABI, "graphics-v1", "plugin-v1")
    release = NodeReleaseV2(revision, base, app_ref, manager, None, artifacts)
    return NodeUpload(tag, release, encode_node_release(release),
                      {f"{role}.bin": data for role, data in bodies.items()})


class Upstream:
    """A GitHub releases listing and its assets; `entries` keyed by tag, newest listed first."""

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.etag = 0
        self.unreachable: set[str] = set()  # paths answered 503

    def put(self, upload: NodeUpload, *, at: str = "2026-09-30T00:00:00Z", prerelease: bool = False,
            manifest: bytes | None = None, drop: tuple[str, ...] = ()) -> None:
        files = {**upload.files, NODE_RELEASE_MANIFEST: upload.manifest if manifest is None else manifest}
        for name in drop:
            files.pop(name)
        assets = []
        for name, data in files.items():
            path = f"/{upload.tag}/{name}"
            self.files[path] = data
            assets.append({"name": name, "browser_download_url": "https://assets.test" + path,
                           "id": next(_IDS), "updated_at": at})
        self.entries[upload.tag] = {"tag_name": upload.tag, "draft": False,
                                    "prerelease": prerelease, "assets": assets}
        self.etag += 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.unreachable:
            return httpx.Response(503)
        if path == f"/repos/{REPO}/releases":
            return httpx.Response(200, json=list(reversed(self.entries.values())),
                                  headers={"ETag": f'W/"{self.etag}"'})
        data = self.files.get(path)
        return httpx.Response(404) if data is None else httpx.Response(200, content=data)

    def origin(self, *, include_prereleases: bool = False) -> GitHubReleaseOrigin:
        return GitHubReleaseOrigin(REPO, transport=httpx.MockTransport(self.handle),
                                   include_prereleases=include_prereleases)


def listed(upstream: Upstream, **options):
    listing = asyncio.run(upstream.origin(**options).list_releases(etag=None))
    return {release.tag: release for release in listing.releases}


# -- B1: the origin turns one release's problem into that release's state -----------------------


def test_a_valid_node_manifest_is_published_with_its_own_asset_version():
    upstream, upload = Upstream(), node_upload("v2.0.0")
    upstream.put(upload, at="2026-09-30T01:02:03Z")
    release = listed(upstream)["v2.0.0"]
    assert release.node_problem is None and release.node_publication.manifest == upload.manifest
    manifest_asset = next(a for a in upstream.entries["v2.0.0"]["assets"]
                          if a["name"] == NODE_RELEASE_MANIFEST)
    assert release.node_version.asset_id == manifest_asset["id"]
    assert release.node_version.changed_at == 1790730123.0


@pytest.mark.parametrize("cause, problem", [
    ("malformed", "node_release_invalid"),
    ("too_large", "node_release_invalid"),
    ("tag_mismatch", "node_release_tag_mismatch"),
    ("missing_artifact", "node_artifact_missing"),
    ("manifest_gone", "node_manifest_missing"),
])
def test_one_bad_node_manifest_is_that_release_problem_and_the_others_still_list(cause, problem):
    upstream, good, bad = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    upstream.put(good)
    if cause == "malformed":
        upstream.put(bad, manifest=b'{"schema": "nope"}')
    elif cause == "too_large":
        upstream.put(bad, manifest=b"{" + b" " * 40_000 + b"}")
    elif cause == "tag_mismatch":
        upstream.put(bad, manifest=node_upload("v9.9.9").manifest)
    elif cause == "missing_artifact":
        upstream.put(bad, drop=("node-display-deb.bin",))
    else:
        upstream.put(bad)
        del upstream.files[f"/v2.1.0/{NODE_RELEASE_MANIFEST}"]
    releases = listed(upstream)
    assert releases["v2.0.0"].node_publication is not None
    assert releases["v2.1.0"].node_publication is None
    assert releases["v2.1.0"].node_problem == problem
    assert releases["v2.1.0"].node_version is not None  # the refusal is versioned too


def test_a_transport_failure_still_aborts_the_whole_listing():
    upstream, upload = Upstream(), node_upload("v2.0.0")
    upstream.put(upload)
    upstream.unreachable.add(f"/v2.0.0/{NODE_RELEASE_MANIFEST}")
    with pytest.raises(OriginUnavailable, match="manifest_unavailable"):
        listed(upstream)


@pytest.mark.parametrize("include_prereleases", [False, True])
def test_a_node_prerelease_is_listed_whatever_the_legacy_flag(include_prereleases):
    upstream = Upstream()
    upstream.put(node_upload("v2.1.0-rc.1"), prerelease=True)
    release = listed(upstream, include_prereleases=include_prereleases)["v2.1.0-rc.1"]
    assert release.is_prerelease and release.node_publication is not None
    # With the legacy flag off its legacy facts were not read: no V1 row is ever written.
    assert release.legacy is include_prereleases


def test_a_prerelease_without_a_node_manifest_stays_unlisted_when_the_flag_is_off():
    upstream = Upstream()
    upstream.entries["v2.1.0-rc.1"] = {"tag_name": "v2.1.0-rc.1", "draft": False,
                                       "prerelease": True, "assets": []}
    assert listed(upstream) == {}


# -- B2: the sync ingests each release in its own transaction -----------------------------------


def make_sync_world(registry, tmp_path):
    """A sync world (`test_content_catalog_sync.make_world`) whose origin is `upstream`'s."""
    from test_content_catalog_sync import make_world

    from central.kernel.ports import ReleaseListing
    build = make_world(registry, tmp_path)

    def at(upstream: Upstream, **options):
        return build(ReleaseListing((), None, unchanged=False), origin=upstream.origin(), **options)
    return at


@pytest.fixture
def sync_world(registry, tmp_path):
    return make_sync_world(registry, tmp_path)


def sync(world) -> None:
    from central.kernel.job_types import SyncReleases
    asyncio.run(world.handler.handle(SyncReleases()))


def observations(registry) -> dict[str, dict]:
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT * FROM node_release_observations").fetchall()
    return {row["tag"]: row for row in rows}


def count(registry, table: str) -> int:
    with registry.db.transaction() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def sessions_for(registry):
    from central.fleet.node_sessions import NodeControlConfig, NodeSessions
    return NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))


def read(registry, world=None):
    from central.fleet.node_release_catalog import NodeReleaseCatalog
    return NodeReleaseCatalog(sessions_for(registry),
                              readiness=world.stored if world else None).list()


def test_tracer_one_valid_one_malformed_one_conflicting_release_in_one_run(registry, sync_world):
    from central.fleet.node_boot import NodeBootService
    from contracts.node_boot import NodeBootRequestV2
    upstream = Upstream()
    first = node_upload("v2.0.0")
    upstream.put(first, at="2026-09-30T00:00:00Z")
    world = sync_world(upstream)
    sync(world)  # v2.0.0 ingested at its first upload
    valid, malformed = node_upload("v2.1.0"), node_upload("v2.2.0")
    recut = node_upload("v2.0.0", salt="-recut")  # same tag and revision, other bytes
    upstream.put(valid)
    upstream.put(malformed, manifest=b"[]")
    upstream.put(recut, at="2026-09-30T02:00:00Z")
    sync(world)
    assert world.reads.etag() == f'W/"{upstream.etag}"'  # the tick finished: ETag stored
    seen = observations(registry)
    assert seen["v2.1.0"]["manifest_sha256"] == valid.sha and seen["v2.1.0"]["problem"] is None
    assert seen["v2.2.0"]["manifest_sha256"] is None
    assert seen["v2.2.0"]["problem"] == "node_release_invalid"
    # The re-cut is refused; the tag keeps its last good upload (and its deployment).
    assert seen["v2.0.0"]["manifest_sha256"] == first.sha
    assert seen["v2.0.0"]["problem"] == "node_release_identity_conflict"
    assert count(registry, "node_deployments") == 2
    rows = read(registry)["releases"]
    assert [(row["tag"], row["deployment_id"], row["problem"]) for row in rows] == [
        ("v2.2.0", None, "node_release_invalid"),
        ("v2.1.0", str(valid.deployment_id), None),
        ("v2.0.0", str(first.deployment_id), "node_release_identity_conflict")]
    boots = NodeBootService(sessions_for(registry))
    boots.select(valid.deployment_id, 0)
    offer = boots.offer(NodeBootRequestV2("abcdef1234567890", UUID(int=7), "b" * 64))
    assert offer.base == valid.release.base
    assert offer.app_environment == valid.release.app_environment


def test_ingest_writes_references_with_the_derived_id_and_never_selects(registry, sync_world):
    upstream, upload = Upstream(), node_upload("v2.0.0", app=False)
    upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    with registry.db.transaction() as conn:
        assets = conn.execute("SELECT kind, identity FROM node_deployment_assets "
                              "WHERE deployment_id=%s ORDER BY kind", (upload.deployment_id,)).fetchall()
        assert conn.execute("SELECT 1 FROM node_boot_policy").fetchone() is None
    assert [(row["kind"], row["identity"]) for row in assets] == [
        ("os-image", upload.release.base.content_key),
        ("sealed-environment", upload.release.manager_primary.environment_sha256)]
    assert upload.deployment_id.hex[12] == "8"  # UUIDv8; no app: variant bit clear
    # A second tick of the same bytes is a no-op (deterministic id: the writer's duplicate path).
    sync(world)
    assert count(registry, "node_deployments") == 1


def test_an_older_observation_writes_nothing(registry, sync_world):
    upstream = Upstream()
    newer = node_upload("v2.0.0", revision="b" * 40, salt="-b")
    older = node_upload("v2.0.0", revision="a" * 40)
    upstream.put(newer, at="2026-09-30T02:00:00Z")
    world = sync_world(upstream)
    sync(world)
    upstream.put(older, at="2026-09-30T01:00:00Z")  # a stale listing lands after the fresh one
    sync(world)
    assert observations(registry)["v2.0.0"]["manifest_sha256"] == newer.sha
    assert count(registry, "node_release_catalog") == 1  # the older catalog row rolled back too
    assert count(registry, "node_deployments") == 1


def test_a_later_valid_upload_clears_the_problem_and_moves_the_pointer(registry, sync_world):
    upstream, good = Upstream(), node_upload("v2.0.0", revision="b" * 40, salt="-b")
    upstream.put(node_upload("v2.0.0"), manifest=b"{}", at="2026-09-30T01:00:00Z")
    world = sync_world(upstream)
    sync(world)
    assert observations(registry)["v2.0.0"]["problem"] == "node_release_invalid"
    upstream.put(good, at="2026-09-30T02:00:00Z")
    sync(world)
    seen = observations(registry)["v2.0.0"]
    assert (seen["manifest_sha256"], seen["problem"]) == (good.sha, None)


def test_a_console_published_deployment_converges_as_a_duplicate(registry, sync_world):
    from central.fleet.node_boot import NodeBootService
    upstream, upload = Upstream(), node_upload("v2.0.0")
    upstream.put(upload)
    world = sync_world(upstream)
    with registry.db.transaction() as conn:  # the console's former publish: same row, same id
        locators = {role: {"url": f"https://assets.test/v2.0.0/{role}.bin", "sha256": a.sha256,
                           "size": a.size_bytes}
                    for role, a in ((a.role, a) for a in upload.release.artifacts)}
        conn.execute("INSERT INTO node_release_catalog VALUES(%s,%s,%s,%s,%s,1000)",
                     (upload.sha, "v2.0.0", upload.release.revision, upload.manifest,
                      json.dumps(locators)))
    NodeBootService(sessions_for(registry)).publish(
        deployment_for_release(upload.sha, upload.release, locators))
    sync(world)
    seen = observations(registry)["v2.0.0"]
    assert (seen["manifest_sha256"], seen["problem"]) == (upload.sha, None)
    assert count(registry, "node_deployments") == 1


def test_a_hand_deployment_holding_the_derived_id_with_other_contents_is_that_tag_problem(
        registry, sync_world):
    from central.content_catalog.deployment import encode_node_deployment
    upstream, upload, other = Upstream(), node_upload("v2.0.0"), node_upload("v2.5.0")
    upstream.put(upload)
    world = sync_world(upstream)
    squatter = replace(deployment_for_release(other.sha, other.release, {
        role: {"url": "https://example.invalid/" + role} for role in ("app", "manager-primary")}),
        deployment_id=upload.deployment_id)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_deployments VALUES(%s,%s,1)",
                     (upload.deployment_id, encode_node_deployment(squatter)))
    sync(world)
    assert observations(registry)["v2.0.0"]["problem"] == "node_deployment_identity_conflict"
    assert observations(registry)["v2.0.0"]["manifest_sha256"] is None


# -- B6: first-run auto-select -------------------------------------------------------------------


def policy(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT revision, deployment_id, previous_deployment_id "
                            "FROM node_boot_policy").fetchone()


def _fail(world, upload):
    """A terminal outcome on one of `upload`'s (absent) files: its readiness is Failed."""
    from central.infra.outcomes import JobOutcomes
    from central.kernel.job_types import FetchOsImage
    with world.reads.transactions.begin() as tx:
        JobOutcomes().record(tx, FetchOsImage(tarball_sha256=upload.release.base.content_key),
                             status="terminal", reason="all_references_rejected",
                             retry_not_before=None, now=world.clock.utc())


def test_first_run_selects_the_newest_stable_release_once_it_is_ready(registry, sync_world):
    from test_node_release_catalog import store_files
    upstream, older, newest = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    rc = node_upload("v2.2.0-rc.1")
    for upload in (older, newest):
        upstream.put(upload)
    upstream.put(rc, prerelease=True)
    world = sync_world(upstream)
    sync(world)
    store_files(world, older)
    store_files(world, rc)
    sync(world)
    # The older release finished first; the newest is still downloading: nothing is selected.
    assert policy(registry) is None
    assert read(registry, world)["selection"]["auto"]["tag"] == "v2.1.0"
    store_files(world, newest)
    sync(world)
    row = policy(registry)
    # A pre-release is never chosen; the selection is created at revision 1 with no previous.
    assert (row["revision"], row["deployment_id"], row["previous_deployment_id"]) == (
        1, newest.deployment_id, None)
    assert read(registry, world)["selection"]["auto"] is None


def test_a_failed_newest_release_falls_back_to_the_next_newest(registry, sync_world):
    from test_node_release_catalog import store_files
    upstream, older, newest = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    for upload in (older, newest):
        upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    _fail(world, newest)
    auto = read(registry, world)["selection"]["auto"]
    assert (auto["tag"], auto["readiness"]) == ("v2.0.0", "downloading")
    store_files(world, older)
    sync(world)
    assert policy(registry)["deployment_id"] == older.deployment_id


def test_with_every_candidate_failed_the_empty_state_names_the_newest_failure(registry, sync_world):
    upstream, upload = Upstream(), node_upload("v2.0.0")
    upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    _fail(world, upload)
    auto = read(registry, world)["selection"]["auto"]
    assert (auto["tag"], auto["readiness"], auto["readiness_reason"]) == (
        "v2.0.0", "failed", "all_references_rejected")
    sync(world)
    assert policy(registry) is None


def test_an_existing_selection_is_never_moved(registry, sync_world):
    from test_node_release_catalog import store_files

    from central.fleet.node_boot import NodeBootService
    upstream, older, newest = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    for upload in (older, newest):
        upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    NodeBootService(sessions_for(registry)).select(older.deployment_id, 0)
    store_files(world, newest)
    sync(world)
    row = policy(registry)
    assert (row["revision"], row["deployment_id"]) == (1, older.deployment_id)


def test_auto_select_racing_an_operator_put_at_revision_zero_writes_exactly_one_selection(
        registry, sync_world):
    import threading

    from test_node_release_catalog import store_files

    from central.fleet.node_boot import NodeBootService
    from central.fleet.node_sessions import NodeControlError
    from central.infra.asset_roots import FLEET_ASSET_LOCK
    upstream, older, newest = Upstream(), node_upload("v2.0.0"), node_upload("v2.1.0")
    for upload in (older, newest):
        upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    store_files(world, newest)
    outcomes: list = []

    def operator():
        try:
            NodeBootService(sessions_for(registry)).select(older.deployment_id, 0)
            outcomes.append("operator")
        except NodeControlError as error:
            outcomes.append(error.code)

    with registry.db.transaction() as blocker:  # both writers queue on the asset-roots lock
        blocker.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))
        threads = [threading.Thread(target=operator), threading.Thread(target=lambda: sync(world))]
        for thread in threads:
            thread.start()
        threading.Event().wait(0.3)
    for thread in threads:
        thread.join(30)
    row = policy(registry)
    assert row["revision"] == 1  # exactly one write
    if outcomes == ["operator"]:
        assert row["deployment_id"] == older.deployment_id  # auto-select yielded
    else:
        assert outcomes == ["node_boot_policy_conflict"]
        assert row["deployment_id"] == newest.deployment_id


# -- B3: the window downloads in the background ----------------------------------------------------


def test_prefetch_fetches_wanted_files_first_and_window_only_files_in_the_background(
        registry, sync_world):
    from fakes.publisher import RecordingPublisher

    from central.assets.handlers import PrefetchHandler
    from central.fleet.node_boot import NodeBootService
    from central.kernel.job_types import BACKGROUND_PRIORITY, FetchOsImage, Prefetch
    from contracts.time import ManualClock
    upstream = Upstream()
    uploads = [node_upload(tag) for tag in ("v2.0.0", "v2.1.0", "v2.2.0", "v2.3.0")]
    for upload in uploads:
        upstream.put(upload)
    world = sync_world(upstream)
    sync(world)
    NodeBootService(sessions_for(registry)).select(uploads[0].deployment_id, 0)  # out of window
    publisher = RecordingPublisher(ManualClock(0))
    handler = PrefetchHandler(catalog=world.catalog, readiness=world.stored,
                              transactions=world.reads.transactions, publisher=publisher)
    asyncio.run(handler.handle(Prefetch()))
    bases = [call.job.tarball_sha256 for call in publisher.calls if isinstance(call.job, FetchOsImage)]
    # The selected release first at its type's priority, then the window, newest release first.
    assert bases == [u.release.base.content_key for u in (uploads[0], uploads[3], uploads[2],
                                                          uploads[1])]
    priorities = {call.job: call.priority for call in publisher.calls}
    assert priorities[FetchOsImage(tarball_sha256=uploads[0].release.base.content_key)] is None
    assert all(priorities[FetchOsImage(tarball_sha256=u.release.base.content_key)]
               == BACKGROUND_PRIORITY for u in uploads[1:])
