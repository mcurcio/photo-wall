"""0012 bead 3: empty-state bootstrap + boot re-hydrate + stray-temp sweep.

Exercised against a REAL Postgres schema (the `registry` fixture, which skips
without PHOTO_WALL_TEST_DATABASE_URL); only the GitHub network boundary is mocked
(via `httpx.MockTransport` -- an empty release list for the .deb autopull, and a
real base tarball for the fetch that caches a bootstrap image). No mock of our
own code.

The three bead-3 steps ride inside `boot_autopull`, gated on a `base_root`, and
never disturb its 0010 `.deb`/`promoted_tag` outcome (asserted here too). Each
acceptance criterion (a-c) and its mutation probe carries a comment naming what
it catches, so the suite is not theater.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile

import httpx
from fastapi.testclient import TestClient
from test_app_release_tasks import _service  # offline-GitHub-wired AppReleaseService
from test_github_releases import Server  # offline GitHub double

from central.app import create_app
from central.app_release_boot import boot_autopull
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.github_releases import GithubReleaseSource
from central.installation_repository import PostgresInstallationRepository
from central.netboot_base import (
    SERIAL_HEADER,
    base_file_path,
    fetch_base,
)

ADMIN = "netboot-base-boot-operator-" + "x" * 32
BASE_URL = "https://example.test/download/photo-wall-base.tar.gz"
SERIAL = "10000000abcd0030"


class _RecordingQueue:
    """Records every enqueue the boot path makes -- base fetches (bead 3) and the
    unchanged 0010 .deb mirror -- with no worker, so a test can assert WHAT boot
    scheduled. Every method returns a `QueueReceipt`, faithful to the real
    `AppReleaseTaskQueue` / `ProcrastinateAppReleaseQueue` contract (which the
    0010 mirror-queued path reads `.coalesced` off) -- never None."""

    def __init__(self) -> None:
        self.base_fetches: list[str] = []
        self.mirrors: list[str] = []

    def enqueue_base_fetch_in(self, conn, tag: str) -> QueueReceipt:
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag: str) -> QueueReceipt:
        self.mirrors.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn) -> QueueReceipt:  # unused by boot; protocol completeness
        return QueueReceipt(coalesced=False)


def _add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _tarball(squashfs):
    sq_sha = hashlib.sha256(squashfs).hexdigest()
    sums = f"{sq_sha}  ./photo-wall-base.squashfs\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add(tar, "photo-wall-base/photo-wall-base.squashfs", squashfs)
        _add(tar, "photo-wall-base/SHA256SUMS", sums)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _source(tarball):
    def handle(request):
        return httpx.Response(200, content=tarball, headers={"Content-Length": str(len(tarball))})

    return GithubReleaseSource("owner/repo", transport=httpx.MockTransport(handle))


def _seed_release(db, clock, tag, squashfs):
    """Discover `tag` WITH base facts (no bytes cached yet); return its tarball."""
    tarball, tsha = _tarball(squashfs)
    AppReleases(db, clock).upsert_discovered(
        tag,
        asset_sha256="a" * 64,
        asset_size=10,
        asset_url="https://example.test/app.deb",
        base_revision="0" * 40,
        base_tarball_sha256=tsha,
        base_tarball_size=len(tarball),
        base_tarball_url=BASE_URL,
    )
    return tarball


def _cache(db, clock, tag, tarball, base_root):
    """Actually fetch+install `tag`'s bytes so a serve returns 200."""

    async def run():
        source = _source(tarball)
        try:
            await fetch_base(source, db, clock, tag, base_root)
        finally:
            await source.close()

    asyncio.run(run())


def _insert_device(db, device_id, **cols):
    cols.setdefault("first_seen", 1000.0)
    cols.setdefault("last_seen", 1000.0)
    names = ["device_id", *cols]
    placeholders = ",".join(["%s"] * len(names))
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO devices({','.join(names)}) VALUES({placeholders})",
            (device_id, *cols.values()),
        )


def _installs(registry) -> PostgresInstallationRepository:
    return PostgresInstallationRepository(registry.clock)


# -- (a) empty-state bootstrap -----------------------------------------------


def test_a_empty_cluster_boots_bootstrap_and_serves_it(registry, tmp_path):
    # (a) No non-retired device has ever been healthy -> the frontier is empty,
    # so boot fetches latest-discovered (an UNVERIFIED bootstrap) and a device
    # netboot then serves it. The 0010 .deb autopull runs UNCHANGED alongside.
    db, clock = registry.db, registry.clock
    squashfs = b"bootstrap base squashfs payload " * 8
    tarball = _seed_release(db, clock, "v0.9.0", squashfs)  # base facts, no bytes yet

    server = Server()
    server.deployable("v1.0.0", deb_body=b"deb")  # a .deb release WITHOUT base facts
    svc = _service(registry, server, tmp_path)
    queue = _RecordingQueue()

    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue,
                                       base_root=tmp_path))

    # 0010 .deb path UNCHANGED: it still selected + deferred the latest .deb tag.
    assert result["pulled"] is False and result["reason"] == "mirror_queued"
    assert result["tag"] == "v1.0.0" and queue.mirrors == ["v1.0.0"]
    # bead 3: the empty-state bootstrap fetch is the base-facts tag, not the
    # base-less .deb tag. PROBE (only fetch when the frontier is empty): with a
    # healthy device seeded, bootstrap would be None -- see the next test.
    assert result["base"]["bootstrap"] == "v0.9.0"
    assert queue.base_fetches == ["v0.9.0"]

    # Worker-drain stand-in: cache the bootstrap bytes, then a Pi netboots it.
    _cache(db, clock, "v0.9.0", tarball, tmp_path)
    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_RecordingQueue())
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL})
    assert response.status_code == 200 and response.content == squashfs


def test_a_healthy_frontier_suppresses_the_bootstrap_fetch(registry, tmp_path):
    # The bootstrap arm fires ONLY on an empty frontier: once any non-retired
    # device is known-good, latest-verified is non-empty and bootstrap is None
    # (a verified fleet is never handed an unverified image). This pins the
    # empty-state guard the previous test's probe references.
    db, clock = registry.db, registry.clock
    _seed_release(db, clock, "v0.9.0", b"x" * 64)   # discoverable, but...
    _seed_release(db, clock, "v1.0.0", b"y" * 64)
    _insert_device(db, "device-healthy", known_good_tag="v1.0.0", known_good_at=1000.0)

    svc = _service(registry, Server(), tmp_path)  # empty .deb server
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue,
                                       base_root=tmp_path))

    assert result["base"]["bootstrap"] is None
    assert queue.base_fetches == []  # no bootstrap, and no cached-but-absent target


# -- (b) boot re-hydrate ------------------------------------------------------


def test_b_rehydrate_reenqueues_a_cached_row_whose_file_is_missing(registry, tmp_path):
    # (b) A base_cache row 'cached' but whose file is ABSENT (persistent-volume
    # wiped / cold start -- the root 503) is re-enqueued at boot. A device
    # known-good on T makes T a re-hydrate target (latest-verified + known-good).
    db, clock = registry.db, registry.clock
    tarball = _seed_release(db, clock, "v1.0.0", b"z" * 96)
    _insert_device(db, "device-a", known_good_tag="v1.0.0", known_good_at=1000.0)
    _cache(db, clock, "v1.0.0", tarball, tmp_path)     # row 'cached', file present
    base_file_path(tmp_path, "v1.0.0").unlink()        # bytes lost across a restart

    svc = _service(registry, Server(), tmp_path)
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue,
                                       base_root=tmp_path))

    assert result["base"]["rehydrated"] == ["v1.0.0"]
    assert queue.base_fetches == ["v1.0.0"]


def test_b_present_file_is_not_reenqueued(registry, tmp_path):
    # (b) Mutation probe: with the file PRESENT the tag is NOT re-enqueued.
    # Removing the absent-file check in boot_rehydrate_targets would re-enqueue it
    # and turn this red.
    db, clock = registry.db, registry.clock
    tarball = _seed_release(db, clock, "v1.0.0", b"z" * 96)
    _insert_device(db, "device-a", known_good_tag="v1.0.0", known_good_at=1000.0)
    _cache(db, clock, "v1.0.0", tarball, tmp_path)     # row 'cached', file PRESENT

    svc = _service(registry, Server(), tmp_path)
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue,
                                       base_root=tmp_path))

    assert result["base"]["rehydrated"] == []
    assert queue.base_fetches == []


# -- (c) stray-temp sweep -----------------------------------------------------


def test_c_stray_temps_are_swept_but_served_files_survive(registry, tmp_path):
    # (c) A crash-before-rename orphan (.base-*.tmp) is removed at boot; a real
    # base-<tag>.squashfs served file is NOT -- the leading-dot temp prefix can
    # never match the final filename by construction.
    stray_tarball = tmp_path / ".base-v0.0.9.tar.deadbeef.tmp"
    stray_squashfs = tmp_path / ".base-abcd1234.tmp"
    stray_tarball.write_bytes(b"partial tarball")
    stray_squashfs.write_bytes(b"partial squashfs")
    served = base_file_path(tmp_path, "v0.0.1")
    served.write_bytes(b"a real served base image")

    svc = _service(registry, Server(), tmp_path)
    queue = _RecordingQueue()
    result = asyncio.run(boot_autopull(svc, svc.packages, _installs(registry), queue,
                                       base_root=tmp_path))

    assert result["base"]["stray_temps"] == 2
    assert not stray_tarball.exists() and not stray_squashfs.exists()
    assert served.exists() and served.read_bytes() == b"a real served base image"
