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
    _CACHING_STALE_SECONDS,
    SERIAL_HEADER,
    base_file_path,
    base_want_needs_fetch,
    device_id_for_serial,
    gc_base_cache,
    latest_verified,
    operator_base_status,
    resolve_base_root,
)
from scripts.package_release_artifacts import package

ADMIN = "netboot-fresh-install-operator-" + "x" * 32
SQUASHFS = b"the rpi-image-gen base squashfs payload, fresh install " * 32
DEB_BYTES = b"synthetic photo-wall-player package bytes " * 64
BOOTSTRAPPER_BYTES = b"synthetic photo-wall-bootstrapper package bytes " * 8
TAG = "v1.2.3"
REVISION = "0" * 40                 # a 40-hex git sha, as build_netboot_bundle emits
SERIAL_CY = "10000000fee10001"     # the involuntary first canary
SERIAL_U = "10000000fee10002"      # a second, unpinned device that must follow

# The mock's release assets are served from `DL_BASE + <filename>`; the code
# composes only the list URL from api_base + repo, so routing on this prefix (and
# nothing else) proves the ONLY surface the mock answers is GitHub.
REPO = "owner/repo"
DL_BASE = "https://example.test/dl/"


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


def _build_outputs(root, squashfs=SQUASHFS):
    """Fixture build outputs shaped exactly like `.github/workflows/base-image.yml`
    emits: the netboot base bundle directory (kernel/initrd/DTB `boot/` tree, the
    base squashfs, and the bundle's own `SHA256SUMS`) plus the Player and
    bootstrapper `.deb`s. The `SHA256SUMS` carries the REAL sha256 of `squashfs`
    (not a placeholder), so the base tarball this feeds into the real packager
    survives `netboot_base.fetch_base`'s extract-and-verify."""
    bundle = root / "base-bundle"
    (bundle / "boot").mkdir(parents=True)
    (bundle / "photo-wall-base.squashfs").write_bytes(squashfs)
    (bundle / "boot" / "config.txt").write_bytes(b"[all]\narm_64bit=1\n")
    (bundle / "boot" / "kernel_2712.img").write_bytes(b"fake-kernel-bytes")
    (bundle / "boot" / "initrd.img").write_bytes(b"fake-initrd-bytes")
    (bundle / "boot" / "bcm2712-rpi-5-b.dtb").write_bytes(b"fake-dtb-bytes")
    sq_sha = hashlib.sha256(squashfs).hexdigest()
    (bundle / "SHA256SUMS").write_bytes(f"{sq_sha}  ./photo-wall-base.squashfs\n".encode())
    player = root / "photo-wall-player_1.2.3_arm64.deb"
    player.write_bytes(DEB_BYTES)
    bootstrapper = root / "photo-wall-bootstrapper_1.2.3_arm64.deb"
    bootstrapper.write_bytes(BOOTSTRAPPER_BYTES)
    return bundle, player, bootstrapper


def _packaged(work, squashfs=SQUASHFS):
    """Run the REAL packager over fixture build outputs; return (release dir,
    manifest dict). The `manifest.json` and `photo-wall-base-<revision>.tar.gz`
    on disk are byte-for-byte what `.github/workflows/release.yml` publishes, so
    the manifest<->asset filename join `_base_image`/`_player_deb` perform is
    exercised for real rather than assumed by a hand-written manifest."""
    bundle, player, bootstrapper = _build_outputs(work / "build", squashfs)
    destination = work / "release"
    manifest = package(bundle, player, bootstrapper, destination, revision=REVISION)
    return destination, manifest


def _release_assets(destination, *, attach_base=True):
    """The `assets[].{name, browser_download_url}` a real GitHub release carries:
    every produced file uploaded under its own name. `attach_base=False` OMITS the
    base tarball asset -- a `.deb`-deployable but base-less release, i.e. the exact
    outage (manifest still names the tarball, but no asset joins to it)."""
    base_name = f"photo-wall-base-{REVISION}.tar.gz"
    assets = []
    for path in sorted(destination.iterdir()):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue  # the release-level SHA256SUMS is not referenced by any join
        if path.name == base_name and not attach_base:
            continue
        assets.append({"name": path.name, "browser_download_url": DL_BASE + path.name})
    return assets


def _github_mock(destination, *, attach_base=True):
    """The single mock boundary: an ``httpx.MockTransport`` that answers ONLY the
    GitHub surfaces (the releases list and each release asset, served from the REAL
    packaged bytes on disk). Any other path raises, so the test proves nothing else
    is mocked. `attach_base=False` reproduces a base-less release."""
    files = {path.name: path.read_bytes()
             for path in destination.iterdir() if path.is_file()}
    releases = json.dumps([{
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "assets": _release_assets(destination, attach_base=attach_base),
    }]).encode()

    def handle(request):
        url = str(request.url)
        if request.url.path == f"/repos/{REPO}/releases":
            return httpx.Response(200, content=releases,
                                  headers={"Content-Length": str(len(releases))})
        name = request.url.path.rsplit("/", 1)[-1]
        if url.startswith(DL_BASE) and name in files:
            body = files[name]
            return httpx.Response(200, content=body,
                                  headers={"Content-Length": str(len(body))})
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
    destination, manifest = _packaged(tmp_path / "pkg")
    tarball_sha = manifest["base_image"]["sha256"]
    tarball_url = DL_BASE + manifest["base_image"]["filename"]
    squashfs_sha = hashlib.sha256(SQUASHFS).hexdigest()
    service = _service(db, clock, app_root, _github_mock(destination))
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
        assert row["base_tarball_url"] == tarball_url  # the real manifest<->asset join

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
    # Resolve the empty-state bootstrap target from the REAL catalog: the LATEST
    # `.deb`-deployable non-prerelease release (`asset_sha256` present and not
    # `undeployable`) -- the release a fresh Pi would actually be handed.
    with db.transaction() as conn:
        latest = conn.execute(
            "SELECT tag, base_tarball_url, base_tarball_sha256 FROM app_releases "
            "WHERE asset_sha256 IS NOT NULL AND mirror_state != 'undeployable' "
            "AND is_prerelease = FALSE "
            "ORDER BY major DESC, minor DESC, patch DESC LIMIT 1"
        ).fetchone()
    if latest is None:
        # No `.deb`-deployable release at all (empty repo / all prereleases): an
        # unrelated repo-state condition, nothing to exercise -- a legitimate skip.
        pytest.skip("no .deb-deployable release on the real repo yet (nothing to exercise)")
    # A `.deb`-deployable latest release that carries NO base tarball IS the outage
    # this suite exists to catch: the Player `.deb` ships, but Central's base facts
    # are NULL, no fetch_base is enqueued, `/v1/netboot/base` 503s, and the Pi
    # cannot PXE-boot. `.deb`-deployability and base-deployability are orthogonal;
    # this is a FAILURE, never a skip.
    assert (
        latest["base_tarball_url"] is not None
        and latest["base_tarball_sha256"] is not None
    ), (
        f"latest .deb-deployable release {latest['tag']!r} has NULL "
        "base_tarball_url/base_tarball_sha256: the release shipped the Player .deb "
        "with no base-image tarball asset attached -- a Raspberry Pi PXE-boot "
        "outage (/v1/netboot/base -> 503), not a skippable condition"
    )
    tag = latest["tag"]

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


# -- packaging <-> ingestion contract (network-free, DB-free) -----------------


def test_packaged_release_satisfies_base_ingestion_contract(tmp_path):
    """The release-contract gate: what the packager EMITS must satisfy what
    ingestion REQUIRES, with no network and no DB.

    Runs the REAL `scripts.package_release_artifacts.package(...)` over fixture
    build outputs, then drives the REAL `GithubReleaseSource._base_image` over the
    produced `manifest.json` joined to the release's assets (every produced file
    uploaded under its own name, as `release.yml` does). The manifest<->asset
    filename join must resolve a non-None base tarball URL and sha256 -- the exact
    NULLs that were the shipped outage. The `attach_base=False` half is the built-in
    mutation probe: omit the tarball asset (a base-less release) and the join MUST
    collapse to None, proving this test binds the join rather than asserting a
    constant."""
    destination, manifest = _packaged(tmp_path / "pkg")
    base_name = manifest["base_image"]["filename"]
    assert base_name == f"photo-wall-base-{REVISION}.tar.gz"

    attached = {asset["name"]: asset["browser_download_url"]
                for asset in _release_assets(destination, attach_base=True)}
    revision, sha, size, url = GithubReleaseSource._base_image(manifest, attached)
    assert url is not None, "base tarball asset failed to join to a download URL"
    assert url == DL_BASE + base_name
    assert sha == manifest["base_image"]["sha256"]
    assert isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
    assert size == manifest["base_image"]["size"]
    assert revision == REVISION

    # The player `.deb` join must resolve too: this release is genuinely
    # `.deb`-deployable, so a NULL base above would be the orthogonal-outage bug.
    player = GithubReleaseSource._player_deb(manifest)
    assert player is not None and player[0] in attached

    # Mutation probe (base-less release): drop the tarball asset. The `.deb` still
    # joins, but the base join collapses to None -- exactly the outage condition.
    baseless = {asset["name"]: asset["browser_download_url"]
                for asset in _release_assets(destination, attach_base=False)}
    assert base_name not in baseless
    _, base_sha_missing, _, url_missing = GithubReleaseSource._base_image(manifest, baseless)
    assert url_missing is None, "base join resolved a URL despite no tarball asset attached"
    assert base_sha_missing == manifest["base_image"]["sha256"]  # sha is from manifest, url is not
    assert GithubReleaseSource._player_deb(manifest)[0] in baseless


# -- device-less-fleet tracer proofs (keep-set / self-heal / fallback / gate) --
#
# The tracer's four mutation-bound proofs: a device-less fleet must BOOT and STAY
# bootable across uncached / terminal / roll pressure. Each applies the pressure
# BETWEEN seed and assertion and fails on the exact revert named in its docstring.
# These seed the catalog + cache directly (unlike the full-arc gate above): they
# isolate the resolver / GC / self-heal logic, not the discover->fetch pipeline.


def _seed_release_row(
    conn, tag, major, minor, patch, now, *,
    sha="b" * 64, url="https://example.test/base.tar.gz",
    mirror_state="discovered", is_prerelease=False,
):
    """Insert a discovered app_releases row carrying base facts (no bytes cached)."""
    conn.execute(
        "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
        "base_tarball_sha256,base_tarball_url,mirror_state,discovered_at,updated_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (tag, major, minor, patch, is_prerelease, sha, url, mirror_state, now, now),
    )


def _seed_cached(conn, base_root, tag, squashfs, now):
    """Write `tag`'s squashfs file + a `cached` base_cache row (bytes present)."""
    sha = hashlib.sha256(squashfs).hexdigest()
    base_file_path(base_root, tag).write_bytes(squashfs)
    conn.execute(
        "INSERT INTO base_cache(tag,state,squashfs_sha256,size,updated_at) "
        "VALUES(%s,'cached',%s,%s,%s)",
        (tag, sha, len(squashfs), now),
    )
    return sha


def _make_tarball(members):
    """A gzip tarball of (name, bytes) members -- built by hand so a test can craft
    an archive-integrity fault (a SHA256SUMS that names the wrong squashfs digest)
    the real packager would never emit."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _asset_mock(name, data):
    """A MockTransport that serves ONLY `name` (a single release asset); any other
    request is a hard error, so a fetch-only test proves nothing else is touched."""
    def handle(request):
        if request.url.path.rsplit("/", 1)[-1] == name:
            return httpx.Response(
                200, content=data, headers={"Content-Length": str(len(data))}
            )
        raise AssertionError(f"unexpected request in fetch-only test: {request.url}")

    return httpx.MockTransport(handle)


def test_proof_a_keepset_protects_device_less_fleet_base(registry, tmp_path, monkeypatch):
    # Proof A (keep-set, (f)): a device-less fleet whose only base is cached must
    # NOT have it GC-evicted. FAILS if (f) is reverted: the old keep-set (device
    # tags U latest_verified U caching) is empty on a device-less fleet, so the
    # sole base leaves the keep-set and GC evicts it -> the outage returns.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("proof-a", tmp_path, monkeypatch)
    now = clock.utc()
    with db.transaction() as conn:
        _seed_release_row(conn, TAG, 1, 2, 3, now)
        _seed_cached(conn, base_root, TAG, SQUASHFS, now)

    with db.transaction() as conn:
        evicted = gc_base_cache(conn, base_root, clock=clock)
        row = conn.execute(
            "SELECT state FROM base_cache WHERE tag=%s", (TAG,)
        ).fetchone()
    assert TAG not in evicted            # not evicted for leaving the keep-set
    assert row["state"] == "cached"      # the row still says cached
    assert base_file_path(base_root, TAG).exists()  # and the bytes survive


def test_proof_b_poll_tail_self_heals_evicted_base(registry, tmp_path, monkeypatch):
    # Proof B (self-heal, (e)): a device-less fleet's cached base is externally
    # evicted; the poll tail must re-enqueue it so the next boot is 200 again.
    # FAILS if (e) is reverted: the poll never enqueues the want tag, nothing is
    # drained, and the boot stays 503.
    db, clock = registry.db, registry.clock
    base_root, app_root = _base("proof-b", tmp_path, monkeypatch)
    destination, _manifest = _packaged(tmp_path / "pkg")
    queue = _FakeQueue()
    service = _service(db, clock, app_root, _github_mock(destination))
    service.release_queue = queue
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=queue)

    with TestClient(app) as client:
        asyncio.run(service.poll())                         # discover
        assert asyncio.run(service.fetch_base(TAG))["cached"] is True  # cache it
        # EVICT: drop the row AND unlink the bytes (an external wipe / admin delete).
        with db.transaction() as conn:
            conn.execute("DELETE FROM base_cache WHERE tag=%s", (TAG,))
        base_file_path(base_root, TAG).unlink()
        gone = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
        assert gone.status_code == 503

        # Poll tail self-heals: resolve_unpinned WANTs TAG, which is not servable
        # and has no row -> a coalesced fetch is enqueued.
        queue.base_fetches.clear()
        asyncio.run(service.poll())
        assert TAG in queue.base_fetches                    # (e) enqueued the want

        # Drain the enqueue as the worker would; the next boot is 200 again.
        for tag in list(dict.fromkeys(queue.base_fetches)):
            asyncio.run(service.fetch_base(tag))
        healed = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
        assert healed.status_code == 200 and healed.content == SQUASHFS


def test_proof_c_serve_falls_back_to_cached_eligible(registry, tmp_path, monkeypatch):
    # Proof C (serve fallback, (b)): an empty fleet whose newest discovered base is
    # uncached (here failed+terminal) must serve an OLDER eligible base that IS
    # cached, with that base's Digest -- a 200, never a 503. FAILS if the
    # resolve_unpinned fb branch is reverted: the resolver returns the uncached
    # t_new and the serve seam 503s while a good older base sits cached.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("proof-c", tmp_path, monkeypatch)
    now = clock.utc()
    fb_bytes = b"older cached eligible base payload " * 16
    with db.transaction() as conn:
        _seed_release_row(conn, "v1.1.0", 1, 1, 0, now)
        _seed_release_row(conn, "v1.2.3", 1, 2, 3, now)     # newest, uncached
        fb_sha = _seed_cached(conn, base_root, "v1.1.0", fb_bytes, now)
        conn.execute(                                        # t_new failed+terminal
            "INSERT INTO base_cache(tag,state,error,updated_at,failure_terminal) "
            "VALUES('v1.2.3','failed','base_digest_mismatch',%s,TRUE)",
            (now,),
        )
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=_FakeQueue())
    with TestClient(app) as client:
        resp = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_U})
    assert resp.status_code == 200
    assert resp.content == fb_bytes                          # the OLDER cached base
    assert resp.headers["digest"] == "sha-256=" + base64.b64encode(
        bytes.fromhex(fb_sha)
    ).decode()


def test_proof_d_terminal_fetch_gate_stops_re_enqueue(registry, tmp_path, monkeypatch):
    # Proof D (terminal gate, (d)): an archive-integrity fault (base_digest_mismatch)
    # marks the row failure_terminal=TRUE with NO backoff (next_retry_at IS NULL),
    # and the poll-tail self-heal must NOT re-enqueue it (re-fetching the same bytes
    # only reproduces the fault). FAILS if the terminal classification is reverted
    # (the fault mis-classified as transient): the row would then be
    # failure_terminal=FALSE with next_retry_at = now + backoff, which the DB-state
    # assertions below (failure_terminal is True; next_retry_at is None) catch
    # directly. NOTE: the `base_fetches == []` check alone does NOT distinguish the
    # two under this test's FROZEN ManualClock -- a reverted-to-transient row's
    # next_retry_at (now+30s) sits in the future of every (frozen, now=1000) tick, so
    # base_want_needs_fetch would gate it during backoff too; there is no re-enqueue
    # "hot loop" to observe. It is the DB-state assertions, not the enqueue count,
    # that prove the revert.
    db, clock = registry.db, registry.clock
    base_root, app_root = _base("proof-d", tmp_path, monkeypatch)
    now = clock.utc()
    sq = b"squashfs bytes whose digest will not match the sums " * 8
    bad_sums = (("0" * 64) + "  ./photo-wall-base.squashfs\n").encode()
    tarball = _make_tarball([
        ("photo-wall-base/photo-wall-base.squashfs", sq),
        ("photo-wall-base/SHA256SUMS", bad_sums),
    ])
    name = "photo-wall-base-bad.tar.gz"
    with db.transaction() as conn:
        _seed_release_row(
            conn, TAG, 1, 2, 3, now,
            sha=hashlib.sha256(tarball).hexdigest(), url=DL_BASE + name,
        )
    queue = _FakeQueue()
    service = AppReleaseService(
        db, AppReleases(db, clock), AppPackages(db, clock), app_root,
        lambda: GithubReleaseSource(REPO, transport=_asset_mock(name, tarball)),
        include_prereleases=False, release_queue=queue,
    )

    res = asyncio.run(service.fetch_base(TAG))
    assert res["cached"] is False and res["reason"] == "base_digest_mismatch"
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT state, failure_terminal, next_retry_at FROM base_cache WHERE tag=%s",
            (TAG,),
        ).fetchone()
    assert row["state"] == "failed"
    assert row["failure_terminal"] is True          # archive-integrity -> terminal
    assert row["next_retry_at"] is None             # no backoff for a terminal fault

    for _ in range(3):                              # N poll-tail self-heal ticks
        service._self_heal_base()
    assert queue.base_fetches == []                # gated: never re-enqueued


def test_transient_fetch_backs_off_then_re_enqueues(registry, tmp_path, monkeypatch):
    # Transient-retry (d): a recoverable fault (here a download sha mismatch, NOT
    # an archive-integrity fault) sets failure_terminal=FALSE + a future
    # next_retry_at, so the self-heal skips it DURING the backoff but re-enqueues
    # once it elapses -- the fault is rate-limited, never stranded terminal.
    db, clock = registry.db, registry.clock
    base_root, app_root = _base("proof-transient", tmp_path, monkeypatch)
    now = clock.utc()
    sq_sha = hashlib.sha256(SQUASHFS).hexdigest()
    good_sums = (sq_sha + "  ./photo-wall-base.squashfs\n").encode()
    tarball = _make_tarball([
        ("photo-wall-base/photo-wall-base.squashfs", SQUASHFS),
        ("photo-wall-base/SHA256SUMS", good_sums),
    ])
    name = "photo-wall-base-transient.tar.gz"
    with db.transaction() as conn:                  # WRONG outer sha -> download fails
        _seed_release_row(conn, TAG, 1, 2, 3, now, sha="c" * 64, url=DL_BASE + name)
    service = AppReleaseService(
        db, AppReleases(db, clock), AppPackages(db, clock), app_root,
        lambda: GithubReleaseSource(REPO, transport=_asset_mock(name, tarball)),
        include_prereleases=False, release_queue=_FakeQueue(),
    )

    assert asyncio.run(service.fetch_base(TAG))["cached"] is False
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT failure_terminal, next_retry_at, fetch_attempts "
            "FROM base_cache WHERE tag=%s",
            (TAG,),
        ).fetchone()
    assert row["failure_terminal"] is False         # transient, NOT stranded
    assert row["fetch_attempts"] == 1
    assert row["next_retry_at"] is not None and row["next_retry_at"] > now
    with db.transaction() as conn:                  # gated during backoff, then eligible
        assert base_want_needs_fetch(
            conn, base_root, TAG, now=row["next_retry_at"] - 1
        ) is False
        assert base_want_needs_fetch(
            conn, base_root, TAG, now=row["next_retry_at"]
        ) is True


def test_abandoned_caching_row_re_enqueued_after_stale_window(registry, tmp_path, monkeypatch):
    # Wedged-`caching` self-heal: a worker crash / task-cancel between fetch_base's
    # `caching` write and its terminal cached/failed write leaves the row stuck at
    # `caching` with no live job -- Procrastinate does not re-queue a stalled job and
    # there is no `caching` reaper, so on a device-less fleet the poll-tail self-heal
    # would otherwise never re-fetch and the boot stays 503 forever. base_want_needs_fetch
    # must treat a `caching` row whose updated_at is older than _CACHING_STALE_SECONDS
    # as ABANDONED (needs a fresh, lock-coalesced fetch) while leaving a genuinely
    # FRESH (in-flight) `caching` row alone. FAILS if the staleness guard is reverted
    # (caching -> always False): the abandoned assertion re-opens the outage.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("caching-stale", tmp_path, monkeypatch)
    now = clock.utc()
    with db.transaction() as conn:
        _seed_release_row(conn, TAG, 1, 2, 3, now)
        conn.execute(                                    # a wedged in-flight fetch
            "INSERT INTO base_cache(tag,state,updated_at) VALUES(%s,'caching',%s)",
            (TAG, now),
        )
    with db.transaction() as conn:
        # FRESH (updated_at == now): genuinely in-flight -> NOT re-enqueued.
        assert base_want_needs_fetch(conn, base_root, TAG, now=now) is False
        # One second before the window elapses: still treated as in-flight.
        assert base_want_needs_fetch(
            conn, base_root, TAG, now=now + _CACHING_STALE_SECONDS - 1
        ) is False
        # ABANDONED: updated_at has not advanced within the window -> needs a fetch.
        assert base_want_needs_fetch(
            conn, base_root, TAG, now=now + _CACHING_STALE_SECONDS
        ) is True


# -- /readyz readiness predicate ----------------------------------------------
#
# /readyz layers netboot-servability on top of /healthz's DB + scheduler liveness,
# resolved through the SAME resolve_unpinned the serve route uses, so k8s drops a
# pod that can serve NO boots from rotation without ever tripping a (futile)
# liveness restart. These reuse the DB-backed `registry` fixture (CI-gated: skips
# without PHOTO_WALL_TEST_DATABASE_URL) + the ASGI app the serve tests drive.


def test_readyz_notready_when_deployable_release_but_nothing_servable(
    registry, tmp_path, monkeypatch
):
    # (a) A deployable release exists (base facts discovered) but its bytes are
    # not cached and there is no cached fallback -> resolve_unpinned returns the
    # uncached t_new as served, /readyz is NotReady (503) -- the exact prod bug.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("readyz-a", tmp_path, monkeypatch)
    now = clock.utc()
    with db.transaction() as conn:
        _seed_release_row(conn, TAG, 1, 2, 3, now)   # discovered, no bytes cached
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=_FakeQueue())
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["reason"] == "base_unservable"
    assert body["want_tag"] == TAG


def test_readyz_ready_when_base_servable(registry, tmp_path, monkeypatch):
    # (b) The newest discovered base is cached -> resolve_unpinned returns it as
    # both served and want, /readyz is Ready (200).
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("readyz-b", tmp_path, monkeypatch)
    now = clock.utc()
    with db.transaction() as conn:
        _seed_release_row(conn, TAG, 1, 2, 3, now)
        _seed_cached(conn, base_root, TAG, SQUASHFS, now)
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=_FakeQueue())
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["served_tag"] == TAG
    assert body["want_tag"] == TAG


def test_readyz_ready_when_serving_cached_fallback(registry, tmp_path, monkeypatch):
    # (b, fallback arm) The newest discovered base is uncached but an OLDER
    # eligible base is cached -> resolve_unpinned serves the fallback (served) and
    # still wants t_new. /readyz is Ready (200) because a boot would 200, and the
    # stuck want is left to operator_base_status, not /readyz, to surface.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("readyz-b2", tmp_path, monkeypatch)
    now = clock.utc()
    fb_bytes = b"older cached eligible base payload " * 16
    with db.transaction() as conn:
        _seed_release_row(conn, "v1.1.0", 1, 1, 0, now)
        _seed_release_row(conn, "v1.2.3", 1, 2, 3, now)  # newest, uncached
        _seed_cached(conn, base_root, "v1.1.0", fb_bytes, now)
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=_FakeQueue())
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["served_tag"] == "v1.1.0"   # the cached fallback is served
    assert body["want_tag"] == "v1.2.3"     # while still wanting the newest


def test_readyz_ready_when_no_deployable_release(registry, tmp_path, monkeypatch):
    # (c) An empty catalog: resolve_unpinned -> (None, None). A legitimately empty
    # cluster is Ready (200), not a fault -- there is nothing to fail to serve.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("readyz-c", tmp_path, monkeypatch)
    app = create_app(db, clock, ADMIN, base_root=base_root, release_queue=_FakeQueue())
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["served_tag"] is None
    assert body["want_tag"] is None


def test_operator_base_status_surfaces_retry_fields(registry, tmp_path, monkeypatch):
    # The retry gate must be visible where /readyz cannot show it (a want tag
    # backing off / terminally failed behind a serving fallback): operator_base_status
    # surfaces fetch_attempts, next_retry_at, failure_terminal per cache row.
    db, clock = registry.db, registry.clock
    base_root, _app_root = _base("operator-fields", tmp_path, monkeypatch)
    now = clock.utc()
    with db.transaction() as conn:
        _seed_release_row(conn, TAG, 1, 2, 3, now)
        conn.execute(
            "INSERT INTO base_cache(tag,state,error,updated_at,"
            "fetch_attempts,next_retry_at,failure_terminal) "
            "VALUES(%s,'failed','base_digest_mismatch',%s,3,%s,TRUE)",
            (TAG, now, now + 42.0),
        )
        status = operator_base_status(conn, base_root)
    entry = next(row for row in status["cache"] if row["tag"] == TAG)
    assert entry["fetch_attempts"] == 3
    assert entry["next_retry_at"] == now + 42.0
    assert entry["failure_terminal"] is True
