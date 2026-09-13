"""Operator-API HTTP tests for GitHub release sourcing (0010, bead 4).

The list / promote / refresh routes and the create_app producer wiring
(release store + enqueue port, gated on PHOTO_WALL_APP_ROOT) are exercised
against real PostgreSQL (conftest's `registry` fixture, schema-per-test,
skipped when PHOTO_WALL_TEST_DATABASE_URL is unset -- the same gate as
test_app_releases / test_app_release_tasks) and a FAKE enqueue port, so no
worker, Procrastinate schema, or network is involved. The web side only
enqueues; it never polls or downloads inline (0010).

The final test is 0010's tracer bullet: a real AppReleaseService driving a
FAKE GitHub (httpx.MockTransport, the `Server` double reused from
test_github_releases) discovers a release, the operator promotes it over HTTP,
the worker's mirror streams the real bytes onto a tmp PHOTO_WALL_APP_ROOT, and
GET /v1/app/manifest + GET /v1/app/package/<sha>.deb serve that exact version.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
from fastapi.testclient import TestClient
from test_github_releases import BODY, REPO, Server

from central.app import create_app
from central.app_packages import AppPackages
from central.app_release_queue import QueueReceipt
from central.app_release_service import AppReleaseService
from central.app_releases import AppReleases
from central.github_releases import GithubReleaseSource

ADMIN = "app-release-http-operator-" + "x" * 32
HEADERS = {"Authorization": "Bearer " + ADMIN}


class FakeReleaseQueue:
    """Captures deferred mirror/poll jobs so no worker or Procrastinate schema is
    needed -- the web side's only obligation is that it enqueues, transactionally."""

    def __init__(self) -> None:
        self.mirrors: list[str] = []
        self.polls = 0

    def enqueue_mirror_in(self, conn, tag: str) -> QueueReceipt:
        self.mirrors.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn) -> QueueReceipt:
        self.polls += 1
        return QueueReceipt(coalesced=False)


def _releases(registry) -> AppReleases:
    return AppReleases(registry.db, registry.clock)


def _packages(registry) -> AppPackages:
    return AppPackages(registry.db, registry.clock)


def _seed_discovered(registry, tag, *, sha=None, size=None, url="http://asset"):
    """A discovered (deployable, not-yet-mirrored) release row."""
    _releases(registry).upsert_discovered(
        tag, asset_sha256=sha, asset_size=size, asset_url=url
    )


def _seed_mirrored(registry, tag, sha, size):
    """A mirrored release: bytes registered in app_packages and the row linked."""
    _releases(registry).upsert_discovered(
        tag, asset_sha256=sha, asset_size=size, asset_url="http://a"
    )
    _packages(registry).register(version=tag, sha256=sha, size=size)
    _releases(registry).mark_mirrored(tag, sha)


def _app(registry, tmp_path, queue):
    return create_app(
        registry.db, registry.clock, ADMIN, app_root=tmp_path, release_queue=queue
    )


# -- list -------------------------------------------------------------------


def test_list_returns_semver_ordered_tracked_releases(registry, tmp_path):
    _seed_discovered(registry, "v1.0.0", sha="a" * 64, size=10)
    _seed_discovered(registry, "v2.0.0", sha="b" * 64, size=20)
    _releases(registry).upsert_discovered("v0.5.0")  # no asset -> undeployable

    with TestClient(_app(registry, tmp_path, FakeReleaseQueue())) as client:
        response = client.get("/v1/operator/app/releases", headers=HEADERS)
        assert response.status_code == 200
        rows = response.json()
        assert [r["tag"] for r in rows] == ["v2.0.0", "v1.0.0", "v0.5.0"]
        by_tag = {r["tag"]: r for r in rows}
        assert by_tag["v1.0.0"]["deployable"] is True
        assert by_tag["v1.0.0"]["mirror_state"] == "discovered"
        assert by_tag["v1.0.0"]["current"] is False
        assert by_tag["v0.5.0"]["deployable"] is False
        assert by_tag["v0.5.0"]["mirror_state"] == "undeployable"


# -- promote ----------------------------------------------------------------


def test_promote_already_mirrored_tag_advances_current_synchronously(registry, tmp_path):
    payload = b"already mirrored player .deb bytes"
    sha = hashlib.sha256(payload).hexdigest()
    _seed_mirrored(registry, "v1.2.3", sha, len(payload))
    queue = FakeReleaseQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.post("/v1/operator/app/releases/v1.2.3/promote", headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"status": "promoted"}
        # Synchronous advance: no mirror enqueued, and the manifest now names it.
        assert queue.mirrors == []
        manifest = client.get("/v1/app/manifest").json()
        assert manifest == {"version": "v1.2.3", "sha256": sha, "size": len(payload)}
        # The list reflects the release as current.
        row = next(r for r in client.get("/v1/operator/app/releases", headers=HEADERS).json())
        assert row["tag"] == "v1.2.3" and row["current"] is True


def test_promote_not_yet_mirrored_tag_returns_pending_and_enqueues_mirror(registry, tmp_path):
    _seed_discovered(registry, "v3.0.0", sha="c" * 64, size=99)
    queue = FakeReleaseQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.post("/v1/operator/app/releases/v3.0.0/promote", headers=HEADERS)
        assert response.status_code == 202
        assert response.json() == {"status": "pending"}
        # A tag-keyed mirror was deferred; current is untouched (still unconfigured).
        assert queue.mirrors == ["v3.0.0"]
        assert client.get("/v1/app/manifest").status_code == 503


def test_promote_unknown_tag_is_404(registry, tmp_path):
    queue = FakeReleaseQueue()
    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.post("/v1/operator/app/releases/v9.9.9/promote", headers=HEADERS)
        assert response.status_code == 404
        assert response.json() == {"error": "release_not_found"}
        assert queue.mirrors == []


def test_promote_undeployable_tag_is_409(registry, tmp_path):
    _releases(registry).upsert_discovered("v4.0.0")  # no asset -> undeployable
    queue = FakeReleaseQueue()
    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.post("/v1/operator/app/releases/v4.0.0/promote", headers=HEADERS)
        assert response.status_code == 409
        assert response.json() == {"error": "release_undeployable"}
        assert queue.mirrors == []


# -- refresh ----------------------------------------------------------------


def test_refresh_enqueues_a_coalesced_poll(registry, tmp_path):
    queue = FakeReleaseQueue()
    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.post("/v1/operator/app/releases/refresh", headers=HEADERS)
        assert response.status_code == 202
        assert response.json() == {"status": "polling"}
        assert queue.polls == 1


# -- auth -------------------------------------------------------------------


def test_release_routes_require_admin(registry, tmp_path):
    with TestClient(_app(registry, tmp_path, FakeReleaseQueue())) as client:
        assert client.get("/v1/operator/app/releases").status_code == 401
        assert client.post("/v1/operator/app/releases/v1.0.0/promote").status_code == 401
        assert client.post("/v1/operator/app/releases/refresh").status_code == 401


# -- unconfigured gate ------------------------------------------------------


def test_release_routes_503_when_release_sourcing_unconfigured(registry):
    # No app_root and no enqueue port: create_app builds cleanly (no crash) and
    # every release route answers a clean 503, while manual staging still works.
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        for response in (
            client.get("/v1/operator/app/releases", headers=HEADERS),
            client.post("/v1/operator/app/releases/v1.0.0/promote", headers=HEADERS),
            client.post("/v1/operator/app/releases/refresh", headers=HEADERS),
        ):
            assert response.status_code == 503
            assert response.json() == {"error": "release_sourcing_unconfigured"}


# -- tracer bullet (0010) ---------------------------------------------------


def test_tracer_poll_promote_mirror_then_player_fetches_the_bytes(registry, tmp_path):
    """poll -> discover -> operator promote -> mirror runs -> Player fetches it.

    The web side enqueues the mirror (captured by the fake queue); the worker's
    AppReleaseService.mirror then runs against the same DB and app-root, exactly
    as the real worker would after dequeuing. Proves central serves the mirrored
    bytes end to end from local disk.
    """
    server = Server()
    _, sha, _ = server.deployable("v0.0.1")
    queue = FakeReleaseQueue()

    def factory() -> GithubReleaseSource:
        return GithubReleaseSource(REPO, transport=httpx.MockTransport(server.handle))

    service = AppReleaseService(
        registry.db,
        AppReleases(registry.db, registry.clock),
        AppPackages(registry.db, registry.clock),
        tmp_path,
        factory,
    )

    with TestClient(_app(registry, tmp_path, queue)) as client:
        # 1. Discover via a real poll against the fake GitHub.
        assert asyncio.run(service.poll())["count"] == 1

        # 2. The list shows v0.0.1 deployable, not current.
        row = next(
            r for r in client.get("/v1/operator/app/releases", headers=HEADERS).json()
            if r["tag"] == "v0.0.1"
        )
        assert row["deployable"] is True and row["current"] is False

        # 3. Promote -> pending, mirror enqueued (bytes not yet present).
        promote = client.post("/v1/operator/app/releases/v0.0.1/promote", headers=HEADERS)
        assert promote.status_code == 202 and promote.json() == {"status": "pending"}
        assert queue.mirrors == ["v0.0.1"]

        # 4. The worker runs the deferred mirror: download + verify + register +
        #    reconcile advances current.
        assert asyncio.run(service.mirror("v0.0.1"))["mirrored"] is True

        # 5. The Player fetches the mirrored version and its exact bytes.
        manifest = client.get("/v1/app/manifest")
        assert manifest.status_code == 200
        assert manifest.json() == {"version": "v0.0.1", "sha256": sha, "size": len(BODY)}
        fetched = client.get(f"/v1/app/package/{sha}.deb")
        assert fetched.status_code == 200 and fetched.content == BODY
