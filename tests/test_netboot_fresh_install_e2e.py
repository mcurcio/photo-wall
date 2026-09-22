"""0012 bead 8 -- the genuine fresh-install end-to-end (the test whose absence
let the outage ship).

This drives the WHOLE base-mirror arc against a REAL Central (the ASGI app +
its routes via ``TestClient``) and a REAL Postgres (the ``registry`` fixture,
which skips without ``PHOTO_WALL_TEST_DATABASE_URL``), with EXACTLY ONE mock: the
external GitHub network boundary, injected as an ``httpx.MockTransport`` on the
``GithubReleaseSource`` the worker's ``AppReleaseService`` builds. Everything
else is real:

  * discovery  -- ``service.poll()`` -> ``GithubReleaseSource.list_releases`` ->
    ``_resolve`` reads the release list + ``manifest.json`` and upserts the
    catalog (``app_releases`` base facts). No seeding of the catalog by hand.
  * download   -- ``service.fetch_base(tag)`` -> ``GithubReleaseSource.download``
    streams the REAL tarball the mock serves, bounded + sha-verified.
  * sha-verify / allowlist-extract / content-address / cache row -- the real
    ``netboot_base.fetch_base`` extracts the two prefixed members, verifies the
    inner squashfs against the tarball's ``SHA256SUMS``, mkstemp -> atomic
    renames ``base-<tag>.squashfs``, and writes the ``base_cache`` row.
  * serve route / base-health / frontier advance -- the real ``/v1/netboot/base``
    and ``/v1/player/base-health`` routes against the real DB.

There is NO ``_stage_base``/``_seed_base``-style helper here: nothing pre-writes
a squashfs or a ``base_cache`` row, so the discover -> download -> verify ->
extract -> cache pipeline is exercised for real (the GitHub bytes the mock serves
ARE a real gzip tarball the code really downloads, verifies, and extracts).

Two gates, per the 0012 bead-8 page:
  (a) ``test_fresh_install_arc_deterministic`` -- the httpx.MockTransport run,
      CI-runnable and deterministic; this is the PR blocker. It asserts the
      empty-``BASE_ROOT`` 503 negative (``base_artifact_unavailable`` -- the exact
      503 that was the original outage), then the self-heal to a 200 with the
      correct ``Digest``, base-health advancing the frontier, and a second device
      following latest-verified.
  (b) ``test_fresh_install_arc_against_real_github`` -- the authenticated
      REAL-GitHub run against ``mcurcio/photo-wall`` releases, gated on
      ``PHOTO_WALL_RELEASE_TOKEN``. It SKIPS when the token is unset. NOTE
      (bead-8 report + errata E10): that secret is not wired into any CI
      workflow today, so gate (b) SKIPS in CI -- the owner must add
      ``PHOTO_WALL_RELEASE_TOKEN`` to the relevant workflow for CI to exercise
      the real-GitHub path.
"""

import asyncio
import base64
import hashlib
import io
import json
import os
import tarfile

import httpx
import pytest
from fastapi.testclient import TestClient

from central.app import create_app
from central.app_packages import AppPackages
from central.app_release_queue import QueueReceipt
from central.app_release_service import AppReleaseService
from central.app_releases import AppReleases
from central.github_releases import GithubReleaseSource
from central.netboot_base import (
    SERIAL_HEADER,
    device_id_for_serial,
    latest_verified,
    resolve_base_root,
)

ADMIN = "netboot-fresh-install-operator-" + "x" * 32
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
TARBALL_NAME = "photo-wall-base.tar.gz"
DEB_NAME = "photo-wall-player_1.2.3_all.deb"


class _FakeQueue:
    """Faithful queue fake: records enqueues and returns a real ``QueueReceipt``
    (the queue port's contract), so a 503 miss can assert "a fetch is enqueued"
    without a procrastinate schema. Nothing else about the arc is faked."""

    def __init__(self):
        self.base_fetches = []
        self.mirrors = []

    def enqueue_base_fetch_in(self, conn, tag):
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag):
        self.mirrors.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn):  # pragma: no cover - unused by the base arc
        return QueueReceipt(coalesced=False)


def _add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _real_tarball(squashfs=SQUASHFS):
    """A REAL base tarball: photo-wall-base/{squashfs, SHA256SUMS}. Returns
    (bytes, tarball_sha256, squashfs_sha256) -- the exact shape
    scripts/package_release_artifacts.py produces (arcname prefix + inner
    SHA256SUMS)."""
    sq_sha = hashlib.sha256(squashfs).hexdigest()
    sums = f"{sq_sha}  ./photo-wall-base.squashfs\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add(tar, "photo-wall-base/photo-wall-base.squashfs", squashfs)
        _add(tar, "photo-wall-base/SHA256SUMS", sums)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest(), sq_sha


def _github_mock(tarball, tarball_sha, *, deb=DEB_BYTES):
    """The single mock boundary: an ``httpx.MockTransport`` that answers ONLY the
    GitHub surfaces (the releases list, the manifest asset, the base tarball, the
    .deb). Any other path raises, so the test proves nothing else is mocked."""
    deb_sha = hashlib.sha256(deb).hexdigest()
    manifest = json.dumps({
        "schema": 1,
        "revision": "0" * 40,
        "base_image": {"filename": TARBALL_NAME, "sha256": tarball_sha, "size": len(tarball)},
        "player_deb": {"filename": DEB_NAME, "sha256": deb_sha, "size": len(deb)},
    }).encode()
    releases = json.dumps([{
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "manifest.json", "browser_download_url": MANIFEST_URL},
            {"name": TARBALL_NAME, "browser_download_url": TARBALL_URL},
            {"name": DEB_NAME, "browser_download_url": DEB_URL},
        ],
    }]).encode()

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


def _service(db, clock, app_root, transport):
    """A REAL AppReleaseService whose ONLY injected fake is the GitHub transport.
    source_factory returns a fresh GithubReleaseSource per call (poll/fetch each
    open+close one), exactly as the worker does."""
    return AppReleaseService(
        db,
        AppReleases(db, clock),
        AppPackages(db, clock),
        app_root,
        lambda: GithubReleaseSource(REPO, transport=transport),
        include_prereleases=False,
    )


def _enroll(db, clock, device_id, token, *, epoch=1):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    player_id = "p-" + hashlib.sha256(device_id.encode()).hexdigest()[:32]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO players(id,public_key,token_hash,authority_epoch,registered_at,"
            "last_seen,device_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (player_id, "pk-" + device_id, token_hash, epoch, clock.utc(), clock.utc(), device_id),
        )


def _base(origin_path, tmp_path, monkeypatch):
    """A configured, EMPTY cache root; its os-images/ and apps/ subdirs are the
    domain roots the serve seam and the worker both derive (0013)."""
    cache_root = tmp_path / origin_path
    base_root = cache_root / "os-images"
    app_root = cache_root / "apps"
    base_root.mkdir(parents=True)
    app_root.mkdir(parents=True)
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(cache_root))
    assert resolve_base_root() == base_root  # the serve + worker share this path
    return base_root, app_root


# -- gate (a): the deterministic MockTransport run (the PR blocker) -----------


def test_fresh_install_arc_deterministic(registry, tmp_path, monkeypatch):
    db, clock = registry.db, registry.clock
    base_root, app_root = _base("base", tmp_path, monkeypatch)
    tarball, tarball_sha, squashfs_sha = _real_tarball()
    service = _service(db, clock, app_root, _github_mock(tarball, tarball_sha))
    queue = _FakeQueue()
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=queue)

    with TestClient(app) as client:
        # (1) THE OUTAGE, asserted. A truly fresh install: empty BASE_ROOT, empty
        # catalog. No frontier, no discoverable base -> the exact 503 this feature
        # exists to fix. The negative that would have caught the shipped outage.
        first = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
        assert first.status_code == 503
        assert first.json() == {"error": "base_artifact_unavailable"}
        assert list(base_root.iterdir()) == []  # nothing pre-staged

        # (2) DISCOVERY (real): the worker polls GitHub (mock) and upserts the
        # catalog. Now a base is discoverable but not yet cached.
        poll = asyncio.run(service.poll())
        assert poll["polled"] is True
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT base_tarball_sha256, base_tarball_url FROM app_releases WHERE tag=%s",
                (TAG,),
            ).fetchone()
        assert row["base_tarball_sha256"] == tarball_sha  # discovered from the manifest
        assert row["base_tarball_url"] == TARBALL_URL

        # (3) Retry after discovery but before the fetch lands: a transient,
        # self-healing miss (bytes not cached yet) enqueues a coalesced fetch.
        miss = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
        assert miss.status_code == 503
        assert miss.json() == {"error": "base_artifact_uncached"}
        assert queue.base_fetches == [TAG]  # the lazy backstop enqueued the fetch

        # (4) FETCH (real): download + sha-verify + allowlist-extract +
        # content-address + write the per-version file + cache row. No staging.
        fetched = asyncio.run(service.fetch_base(TAG))
        assert fetched == {"cached": True, "tag": TAG, "sha256": squashfs_sha}
        with db.transaction() as conn:
            cache = conn.execute(
                "SELECT state, squashfs_sha256 FROM base_cache WHERE tag=%s", (TAG,)
            ).fetchone()
        assert cache["state"] == "cached" and cache["squashfs_sha256"] == squashfs_sha
        assert (base_root / f"base-{TAG}.squashfs").read_bytes() == SQUASHFS

        # (5) SELF-HEAL to a 200: the same device's next boot now serves the real
        # extracted bytes with a Digest equal to the recorded squashfs sha AND the
        # sha of the bytes actually served (they agree by construction).
        ok = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
        assert ok.status_code == 200 and ok.content == SQUASHFS
        expected = "sha-256=" + base64.b64encode(bytes.fromhex(squashfs_sha)).decode()
        assert ok.headers["digest"] == expected
        assert ok.headers["digest"] == "sha-256=" + base64.b64encode(
            hashlib.sha256(ok.content).digest()
        ).decode()

        # The 200 recorded the served tag (pending), the precondition for a
        # validated base-health check-in.
        device_id = device_id_for_serial(SERIAL_CY)
        with db.transaction() as conn:
            served = conn.execute(
                "SELECT last_served_tag, boot_outcome FROM devices WHERE device_id=%s",
                (device_id,),
            ).fetchone()
        assert served["last_served_tag"] == TAG and served["boot_outcome"] == "pending"

        # (6) FRONTIER ADVANCE (real): the enrolled device posts base-health on the
        # served tag; known-good advances and latest-verified names that tag.
        token = "fresh-install-token-" + "z" * 24
        _enroll(db, clock, device_id, token)
        health = client.post(
            "/v1/player/base-health",
            json={"authority_epoch": 1, "sequence": 1, "running_tag": TAG, "healthy": True},
            headers={"Authorization": "Bearer " + token},
        )
        assert health.status_code == 200 and health.json() == {"accepted": True}
        with db.transaction() as conn:
            device = conn.execute(
                "SELECT known_good_tag, boot_outcome FROM devices WHERE device_id=%s",
                (device_id,),
            ).fetchone()
            assert latest_verified(conn) == TAG  # the frontier now names the healthy tag
        assert device["known_good_tag"] == TAG and device["boot_outcome"] == "healthy"

        # (7) A SECOND, unpinned device follows latest-verified on its first boot
        # (bytes already cached from the canary's fetch) -- the whole point of a
        # per-device frontier.
        follow = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_U})
        assert follow.status_code == 200 and follow.content == SQUASHFS
        with db.transaction() as conn:
            second = conn.execute(
                "SELECT last_served_tag FROM devices WHERE device_id=%s",
                (device_id_for_serial(SERIAL_U),),
            ).fetchone()
        assert second["last_served_tag"] == TAG  # followed the frontier, no pin


# -- gate (b): the authenticated REAL-GitHub run (skips without the secret) ----


def test_fresh_install_arc_against_real_github(registry, tmp_path, monkeypatch):
    # Gated on PHOTO_WALL_RELEASE_TOKEN: this is the ONLY test that touches the
    # real network, and it authenticates to mcurcio/photo-wall. It SKIPS cleanly
    # when the token is unset -- which is the case in CI today (the secret is not
    # wired into any workflow; see errata E10 + the bead report). No mock: a real
    # GithubReleaseSource against real releases.
    token = os.environ.get("PHOTO_WALL_RELEASE_TOKEN")
    if not token:
        pytest.skip("set PHOTO_WALL_RELEASE_TOKEN to exercise the real-GitHub path (gate b)")
    repo = os.environ.get("PHOTO_WALL_RELEASE_REPO", "mcurcio/photo-wall")
    db, clock = registry.db, registry.clock
    base_root, app_root = _base("base-real", tmp_path, monkeypatch)
    monkeypatch.setenv("PHOTO_WALL_RELEASE_REPO", repo)
    service = AppReleaseService(
        db,
        AppReleases(db, clock),
        AppPackages(db, clock),
        app_root,
        lambda: GithubReleaseSource(repo, token=token),
        include_prereleases=False,
    )

    assert asyncio.run(service.poll())["polled"] is True
    # Resolve the empty-state bootstrap target from the REAL catalog. A repo whose
    # releases predate the base_image manifest field carries no base facts; that is
    # a legitimate skip, not a failure -- gate (b) proves the pipeline against a
    # release that actually ships a base tarball.
    with db.transaction() as conn:
        tag = conn.execute(
            "SELECT tag FROM app_releases WHERE base_tarball_sha256 IS NOT NULL "
            "AND base_tarball_url IS NOT NULL AND is_prerelease = FALSE "
            "ORDER BY major DESC, minor DESC, patch DESC LIMIT 1"
        ).fetchone()
    if tag is None:
        pytest.skip("no released base_image asset on the real repo yet (nothing to mirror)")
    tag = tag["tag"]

    fetched = asyncio.run(service.fetch_base(tag))
    assert fetched["cached"] is True and fetched["tag"] == tag
    served_sha = fetched["sha256"]

    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=_FakeQueue())
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
    assert response.status_code == 200
    assert response.headers["digest"] == "sha-256=" + base64.b64encode(
        bytes.fromhex(served_sha)
    ).decode()
    assert response.headers["digest"] == "sha-256=" + base64.b64encode(
        hashlib.sha256(response.content).digest()
    ).decode()
