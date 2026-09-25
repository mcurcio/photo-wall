"""0012 bead 8 -- the genuine fresh-install end-to-end (the test whose absence
let the outage ship), rewired in P2 to the Central MVP pipeline.

This drives the WHOLE OS-image arc against a REAL Central (the ASGI app + its
routes via ``TestClient``, over the production ``build_content_services``
wiring) and a REAL Postgres (the ``registry`` fixture, which skips without
``PHOTO_WALL_TEST_DATABASE_URL``), with EXACTLY ONE mock: the external GitHub
network boundary, an ``httpx.MockTransport`` on the ``GitHubReleaseOrigin`` the
worker's ``build_job_runtime`` builds. Everything else is real:

  * discovery  -- ``SyncReleases`` runs through the worker's ``JobExecutor``:
    ``GitHubReleaseOrigin.list_releases`` reads the release list + ``manifest.json``,
    and the handler records the release, its Asset references, auto-promotes it and
    publishes the desired fetches. No seeding of the catalog by hand.
  * miss -> wait -> serve (design §6(c)) -- a Pi asks while nothing is on disk; the
    request publishes ``FetchOsImage`` and waits on its handle; the worker's
    executor then runs the fetch (download, verify, allowlist-extract, rename into
    place, produced facts + ``ok`` outcome + NOTIFY), and the SAME request answers
    200 with the right ``Digest``.
  * base-health / frontier advance / an unpinned follower -- the real routes.
  * the Player ``.deb`` -- manifest of the auto-promoted release, then its bytes.

There is NO pre-staging: nothing writes a squashfs or an Asset row by hand, so the
discover -> download -> verify -> extract -> serve pipeline runs for real (the
GitHub bytes the mock serves ARE a real gzip tarball).

Two gates, per the 0012 bead-8 page:
  (a) ``test_fresh_install_arc_deterministic`` -- the httpx.MockTransport run,
      CI-runnable and deterministic; this is the PR blocker. Its first assertion is
      the empty-catalog answer, now ``404 base_unknown`` (decision 4) where the
      outage was a 503.
  (b) ``test_fresh_install_arc_against_real_github`` -- the authenticated
      REAL-GitHub run against ``mcurcio/photo-wall`` releases, gated on
      ``PHOTO_WALL_RELEASE_TOKEN``. It SKIPS when the token is unset (errata E10:
      the secret is not wired into CI).
"""

import asyncio
import base64
import hashlib
import json
import os
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from support.github_release import deb_name, manifest_bytes, real_tarball, release_entry
from support.workers import wait_for

from central import content_wiring
from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.content_wiring import build_content_services, build_job_runtime
from central.kernel.job_types import FetchOsImage, FetchPackage, SyncReleases
from central.netboot_base import SERIAL_HEADER
from central.origins.github import GitHubReleaseOrigin

ADMIN = "netboot-fresh-install-operator-" + "x" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
SQUASHFS = b"the rpi-image-gen base squashfs payload, fresh install " * 32
DEB_BYTES = b"synthetic photo-wall-player package bytes " * 64
TAG = "v1.2.3"
SERIAL_CY = "10000000fee10001"     # the involuntary first canary
SERIAL_U = "10000000fee10002"      # a second, unpinned device that must follow

# The mock GitHub asset URLs. The code composes the list URL from api_base + the
# repo; every OTHER URL is whatever we put in the release JSON's
# `assets[].browser_download_url`, so routing on these exact strings proves the
# ONLY thing the mock answers is GitHub.
REPO = "owner/repo"
MANIFEST_URL = "https://example.test/dl/manifest.json"
TARBALL_URL = "https://example.test/dl/photo-wall-base.tar.gz"
DEB_URL = "https://example.test/dl/photo-wall-player_1.2.3_all.deb"
DEB_NAME = deb_name(TAG)


def _github_mock(tarball, tarball_sha, *, deb=DEB_BYTES):
    """The single mock boundary: an ``httpx.MockTransport`` that answers ONLY the
    GitHub surfaces (the releases list, the manifest asset, the base tarball, the
    .deb). Any other path raises, so the test proves nothing else is mocked."""
    manifest = manifest_bytes(tarball=tarball, tarball_sha=tarball_sha, deb=deb,
                              deb_filename=DEB_NAME)
    releases = json.dumps([release_entry(TAG, manifest_url=MANIFEST_URL, tarball_url=TARBALL_URL,
                                         deb_filename=DEB_NAME, deb_url=DEB_URL)]).encode()

    def handle(request):
        url = str(request.url)
        if request.url.path == f"/repos/{REPO}/releases":
            return httpx.Response(200, content=releases,
                                  headers={"Content-Length": str(len(releases))})
        if url == MANIFEST_URL:
            return httpx.Response(200, content=manifest,
                                  headers={"Content-Length": str(len(manifest))})
        if url == TARBALL_URL:
            return httpx.Response(200, content=tarball,
                                  headers={"Content-Length": str(len(tarball))})
        if url == DEB_URL:
            return httpx.Response(200, content=deb,
                                  headers={"Content-Length": str(len(deb))})
        # Proof there is no other mocked surface: anything else is a hard error.
        raise AssertionError(f"unexpected non-GitHub request in e2e: {url}")

    return httpx.MockTransport(handle)


def _enroll(db, clock, device_id, token, *, epoch=1):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    player_id = "p-" + hashlib.sha256(device_id.encode()).hexdigest()[:32]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO players(id,public_key,token_hash,authority_epoch,registered_at,"
            "last_seen,device_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (player_id, "pk-" + device_id, token_hash, epoch, clock.utc(), clock.utc(), device_id),
        )


def _executor(db, clock, cache_root, monkeypatch, *, transport=None, env=None):
    """The worker's real job execution (`build_job_runtime`, every CATALOG handler), with
    only the GitHub transport swapped when `transport` is given. `execute` is exactly what
    the runtime's procrastinate task body runs for one delivery."""
    if transport is not None:
        class _MockedOrigin:
            @staticmethod
            def from_env(_env):
                return GitHubReleaseOrigin(REPO, transport=transport)

        monkeypatch.setattr(content_wiring, "GitHubReleaseOrigin", _MockedOrigin)
    runtime = build_job_runtime(db, clock, cache_root=cache_root, env=env or {})
    executor = runtime._executor

    def execute(job):
        return asyncio.run(executor.execute(job, 0))

    return execute


def _digest(data):
    return "sha-256=" + base64.b64encode(hashlib.sha256(data).digest()).decode()


# -- gate (a): the deterministic MockTransport run (the PR blocker) -----------


def test_fresh_install_arc_deterministic(registry, tmp_path, monkeypatch):
    db, clock = registry.db, registry.clock
    cache_root = tmp_path / "cache"
    tarball, tarball_sha, squashfs_sha = real_tarball(SQUASHFS)
    execute = _executor(db, clock, cache_root, monkeypatch,
                        transport=_github_mock(tarball, tarball_sha))
    content = build_content_services(db, clock, cache_root=cache_root)
    app = create_app(db, clock, ADMIN, content=content)

    with TestClient(app) as client:
        # (1) A truly fresh install: empty cache, empty catalog. Unknown content is a
        # 404 (decision 4) -- no release exists to produce anything from.
        first = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
        assert first.status_code == 404
        assert first.json() == {"error": "base_unknown"}
        assert not (cache_root / "os-images").exists()  # nothing pre-staged

        # (2) DISCOVERY (real): the worker runs SyncReleases against GitHub (mock).
        assert execute(SyncReleases()) == "ok"
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT base_tarball_sha256, base_tarball_url FROM app_releases WHERE tag=%s",
                (TAG,),
            ).fetchone()
            queued = {r["task_name"] for r in conn.execute(
                "SELECT task_name FROM procrastinate_jobs WHERE status='todo'").fetchall()}
        assert row["base_tarball_sha256"] == tarball_sha  # discovered from the manifest
        assert row["base_tarball_url"] == TARBALL_URL
        # The sync published the desired fetches (the bootstrap image, the promoted .deb).
        assert {"photo_wall.os_image.fetch", "photo_wall.player_deb.fetch"} <= queued

        # (3) MISS -> WAIT -> SERVE (§6(c)): the Pi asks before any worker has run the
        # fetch. Its request publishes FetchOsImage and waits on the job's handle ...
        result = {}
        request = threading.Thread(target=lambda: result.update(
            response=client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})))
        request.start()
        wait_for(lambda: content.feed._entries, seconds=10,  # the request is waiting
                 what="the request to wait on its fetch's outcome")
        # ... a worker runs it (download + verify + allowlist-extract + rename into
        # place; produced facts + ok outcome + NOTIFY) ...
        assert execute(FetchOsImage(tarball_sha256=tarball_sha)) == "ok"
        request.join(30)
        ok = result["response"]
        # ... and the SAME request answers with the extracted bytes and a Digest equal
        # to the sha of the bytes actually served (and to SHA256SUMS in the tarball).
        assert ok.status_code == 200 and ok.content == SQUASHFS
        assert ok.headers["digest"] == _digest(ok.content)
        assert ok.headers["digest"] == "sha-256=" + base64.b64encode(
            bytes.fromhex(squashfs_sha)).decode()
        assert (cache_root / "os-images" / f"base-{tarball_sha}.squashfs").read_bytes() == (
            SQUASHFS)

        # The 200 recorded the served tag (pending), the precondition for a
        # validated base-health check-in.
        device_id = device_id_for_serial(SERIAL_CY)
        with db.transaction() as conn:
            served = conn.execute(
                "SELECT last_served_tag, boot_outcome FROM devices WHERE device_id=%s",
                (device_id,),
            ).fetchone()
        assert served["last_served_tag"] == TAG and served["boot_outcome"] == "pending"

        # (4) FRONTIER ADVANCE (real): the enrolled device posts base-health on the
        # served tag; known-good advances and the frontier names that tag.
        token = "fresh-install-token-" + "z" * 24
        _enroll(db, clock, device_id, token)
        health = client.post(
            "/v1/player/base-health",
            json={"authority_epoch": 1, "sequence": 1, "running_tag": TAG, "healthy": True},
            headers={"Authorization": "Bearer " + token},
        )
        assert health.status_code == 200 and health.json() == {"accepted": True}
        view = client.get("/v1/operator/netboot", headers=AUTH).json()
        assert view["frontier"] == TAG  # the frontier now names the healthy tag
        (canary,) = [d for d in view["devices"] if d["device_id"] == device_id]
        assert canary["known_good_tag"] == TAG and canary["boot_outcome"] == "healthy"

        # (5) A SECOND, unpinned device follows the frontier on its first boot (bytes
        # already cached from the canary's fetch) -- the point of a per-device frontier.
        follow = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_U})
        assert follow.status_code == 200 and follow.content == SQUASHFS
        with db.transaction() as conn:
            second = conn.execute(
                "SELECT last_served_tag FROM devices WHERE device_id=%s",
                (device_id_for_serial(SERIAL_U),),
            ).fetchone()
        assert second["last_served_tag"] == TAG  # followed the frontier, no pin

        # (6) The Player .deb of the auto-promoted release: the manifest needs no bytes;
        # once the worker has fetched it, the package route serves it from disk.
        deb_sha = hashlib.sha256(DEB_BYTES).hexdigest()
        manifest = client.get("/v1/app/manifest")
        assert manifest.status_code == 200
        assert manifest.json() == {"version": TAG, "sha256": deb_sha, "size": len(DEB_BYTES)}
        assert execute(FetchPackage(sha256=deb_sha)) == "ok"
        package = client.get(f"/v1/app/package/{deb_sha}.deb")
        assert package.status_code == 200 and package.content == DEB_BYTES
        assert package.headers["digest"] == _digest(DEB_BYTES)


# -- gate (b): the authenticated REAL-GitHub run (skips without the secret) ----


def test_fresh_install_arc_against_real_github(registry, tmp_path, monkeypatch):
    # Gated on PHOTO_WALL_RELEASE_TOKEN: this is the ONLY test that touches the
    # real network, and it authenticates to mcurcio/photo-wall. It SKIPS cleanly
    # when the token is unset -- which is the case in CI today (the secret is not
    # wired into any workflow; see errata E10). No mock: the worker's real
    # GitHubReleaseOrigin, built from the environment, against real releases.
    token = os.environ.get("PHOTO_WALL_RELEASE_TOKEN")
    if not token:
        pytest.skip("set PHOTO_WALL_RELEASE_TOKEN to exercise the real-GitHub path (gate b)")
    repo = os.environ.get("PHOTO_WALL_RELEASE_REPO", "mcurcio/photo-wall")
    db, clock = registry.db, registry.clock
    cache_root = tmp_path / "cache-real"
    execute = _executor(db, clock, cache_root, monkeypatch, env={
        "PHOTO_WALL_RELEASE_REPO": repo, "PHOTO_WALL_RELEASE_TOKEN": token})
    app = create_app(db, clock, ADMIN,
                     content=build_content_services(db, clock, cache_root=cache_root))

    with TestClient(app) as client:  # the lifespan installs procrastinate's schema
        assert execute(SyncReleases()) == "ok"
        # The empty-state bootstrap target from the REAL catalog. A repo whose releases
        # predate the base_image manifest field carries no base facts; that is a
        # legitimate skip, not a failure.
        with db.transaction() as conn:
            tag = conn.execute(
                "SELECT tag, base_tarball_sha256 FROM app_releases "
                "WHERE base_tarball_sha256 IS NOT NULL "
                "AND base_tarball_url IS NOT NULL AND is_prerelease = FALSE "
                "ORDER BY major DESC, minor DESC, patch DESC LIMIT 1"
            ).fetchone()
        if tag is None:
            pytest.skip("no released base_image asset on the real repo yet (nothing to fetch)")
        assert execute(FetchOsImage(tarball_sha256=tag["base_tarball_sha256"])) == "ok"
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
    assert response.status_code == 200
    assert response.headers["digest"] == _digest(response.content)
