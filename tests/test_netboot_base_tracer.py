"""0012 bead 1 (TRACER): the health -> frontier -> serve arc, end to end.

Exercised against a REAL Postgres schema (the `registry` fixture, which skips
without PHOTO_WALL_TEST_DATABASE_URL); only the GitHub network boundary is
mocked, via `httpx.MockTransport` serving a REAL base tarball (a real gzip tar
carrying a real squashfs + inner SHA256SUMS). No mock of our own code.

Acceptance criteria (1-5) and the four mutation probes each carry a comment
naming the mutation they would catch, so the suite is not theater.
"""

import asyncio
import base64
import hashlib
import io
import tarfile

import httpx
import pytest
from fastapi.testclient import TestClient

from central.app import create_app
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.github_releases import GithubReleaseSource
from central.netboot_base import (
    SERIAL_HEADER,
    BaseRootError,
    assert_base_root_writable,
    device_id_for_serial,
    fetch_base,
    latest_discovered,
    latest_verified,
    resolve_base_root,
)

ADMIN = "netboot-base-tracer-operator-" + "x" * 32
SQUASHFS = b"the rpi-image-gen base squashfs payload " * 16
SERIAL_CY = "10000000abcd0001"
SERIAL_U = "10000000abcd0002"
BASE_URL = "https://example.test/download/photo-wall-base.tar.gz"


class _FakeQueue:
    """Records base-fetch enqueues so a 503-miss can assert "a fetch is enqueued"
    without a worker. Satisfies the AppReleaseTaskQueue port the netboot route
    depends on (only enqueue_base_fetch_in is exercised here)."""

    def __init__(self):
        self.base_fetches = []

    def enqueue_base_fetch_in(self, conn, tag):
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag):  # pragma: no cover - unused by the base path
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn):  # pragma: no cover - unused by the base path
        return QueueReceipt(coalesced=False)


def _add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _tarball(squashfs=SQUASHFS, *, sums_name=None, extra=None):
    """A real base tarball: photo-wall-base/{squashfs, SHA256SUMS}. Returns
    (bytes, tarball_sha256, squashfs_sha256)."""
    sq_sha = hashlib.sha256(squashfs).hexdigest()
    listed = sums_name or "photo-wall-base.squashfs"
    sums = f"{sq_sha}  ./{listed}\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add(tar, "photo-wall-base/photo-wall-base.squashfs", squashfs)
        _add(tar, "photo-wall-base/SHA256SUMS", sums)
        if extra is not None:
            extra(tar)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest(), sq_sha


def _source(tarball):
    def handle(request):
        return httpx.Response(
            200, content=tarball, headers={"Content-Length": str(len(tarball))}
        )

    return GithubReleaseSource("owner/repo", transport=httpx.MockTransport(handle))


def _fetch(tarball, db, clock, tag, base_root):
    async def run():
        source = _source(tarball)
        try:
            return await fetch_base(source, db, clock, tag, base_root)
        finally:
            await source.close()

    return asyncio.run(run())


def _seed_release(db, clock, tag, tarball_sha, tarball_size, *, url=BASE_URL):
    AppReleases(db, clock).upsert_discovered(
        tag,
        asset_sha256="a" * 64,
        asset_size=10,
        asset_url="https://example.test/app.deb",
        base_revision="0" * 40,
        base_tarball_sha256=tarball_sha,
        base_tarball_size=tarball_size,
        base_tarball_url=url,
    )


def _raw_release(conn, tag, major, minor, patch, now):
    """Insert an app_releases row directly (bypasses parse_semver), so a
    non-semver tag whose PK CHECK still passes can be planted for the
    exclusion probes."""
    conn.execute(
        "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,base_tarball_sha256,"
        "base_tarball_url,discovered_at,updated_at) VALUES(%s,%s,%s,%s,FALSE,%s,%s,%s,%s)",
        (tag, major, minor, patch, "b" * 64, "https://example.test/x.tgz", now, now),
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
    return player_id


# -- Acceptance criteria -----------------------------------------------------


def test_criterion1_empty_frontier_serves_503_and_enqueues_a_fetch(registry, tmp_path):
    db, clock = registry.db, registry.clock
    tarball, tsha, _ = _tarball()
    _seed_release(db, clock, "v0.0.2", tsha, len(tarball))  # discovered, not cached
    queue = _FakeQueue()
    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=queue)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
    assert response.status_code == 503
    assert response.json() == {"error": "base_artifact_uncached"}
    # Empty frontier -> resolves latest-discovered -> miss -> a fetch is enqueued.
    assert queue.base_fetches == ["v0.0.2"]


def test_criterion2_after_fetch_serves_200_with_digest_equal_to_recorded_sha(registry, tmp_path):
    db, clock = registry.db, registry.clock
    tarball, tsha, sq_sha = _tarball()
    _seed_release(db, clock, "v0.0.2", tsha, len(tarball))
    _fetch(tarball, db, clock, "v0.0.2", tmp_path)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY})
    assert response.status_code == 200
    assert response.content == SQUASHFS
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT state, squashfs_sha256 FROM base_cache WHERE tag='v0.0.2'"
        ).fetchone()
    assert row["state"] == "cached" and row["squashfs_sha256"] == sq_sha
    expected = "sha-256=" + base64.b64encode(bytes.fromhex(row["squashfs_sha256"])).decode()
    # PROBE (break the Digest computation): the served Digest must equal the
    # recorded squashfs sha AND the sha of the bytes actually served.
    assert response.headers["digest"] == expected
    assert response.headers["digest"] == "sha-256=" + base64.b64encode(
        hashlib.sha256(response.content).digest()
    ).decode()


def test_criterion3_base_health_advances_known_good_and_moves_the_frontier(registry, tmp_path):
    db, clock = registry.db, registry.clock
    tarball, tsha, _ = _tarball()
    _seed_release(db, clock, "v0.0.2", tsha, len(tarball))
    _fetch(tarball, db, clock, "v0.0.2", tmp_path)
    device_id = device_id_for_serial(SERIAL_CY)
    token = "z" * 40
    _enroll(db, clock, device_id, token)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        assert client.get(
            "/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY}
        ).status_code == 200  # records last_served_tag=v0.0.2 pending
        health = client.post(
            "/v1/player/base-health",
            json={"authority_epoch": 1, "sequence": 1, "running_tag": "v0.0.2", "healthy": True},
            headers={"Authorization": "Bearer " + token},
        )
    assert health.status_code == 200 and health.json() == {"accepted": True}
    with db.transaction() as conn:
        device = conn.execute(
            "SELECT known_good_tag, boot_outcome FROM devices WHERE device_id=%s", (device_id,)
        ).fetchone()
        # The frontier now names the tag the device confirmed healthy.
        assert latest_verified(conn) == "v0.0.2"
    assert device["known_good_tag"] == "v0.0.2" and device["boot_outcome"] == "healthy"


def test_criterion4_second_unpinned_device_follows_latest_verified(registry, tmp_path):
    db, clock = registry.db, registry.clock
    tarball, tsha, _ = _tarball()
    _seed_release(db, clock, "v0.0.2", tsha, len(tarball))
    _fetch(tarball, db, clock, "v0.0.2", tmp_path)
    # A first device already healthy on v0.0.2 (the frontier input).
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,known_good_tag,known_good_at) "
            "VALUES('device-cy',%s,%s,'v0.0.2',%s)",
            (clock.utc(), clock.utc(), clock.utc()),
        )
    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_U})
    assert response.status_code == 200 and response.content == SQUASHFS
    with db.transaction() as conn:
        served = conn.execute(
            "SELECT last_served_tag FROM devices WHERE device_id=%s",
            (device_id_for_serial(SERIAL_U),),
        ).fetchone()
    assert served["last_served_tag"] == "v0.0.2"  # followed latest-verified, not a pin


def test_criterion5_boot_assertion_fails_loud_when_base_root_absent_or_unwritable(tmp_path):
    # Resolve is a pure env read; the assertion is the FAIL-LOUD (raises, never a
    # silent later 503).
    assert resolve_base_root({}) is None
    assert resolve_base_root({"PHOTO_WALL_BASE_ROOT": str(tmp_path)}) == tmp_path
    with pytest.raises(BaseRootError):
        assert_base_root_writable(None)
    with pytest.raises(BaseRootError):
        assert_base_root_writable(tmp_path / "does-not-exist")
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        with pytest.raises(BaseRootError):
            assert_base_root_writable(read_only)
    finally:
        read_only.chmod(0o700)
    assert assert_base_root_writable(tmp_path) == tmp_path


# -- Mutation probes ---------------------------------------------------------


def test_probe_503_miss_records_no_last_served(registry, tmp_path):
    # PROBE (record last_served/pending on the 503 branch): a cache-miss 503 must
    # leave NO last-served record, so a self-healing retry is not mistaken for a
    # failed boot. If the serve wrote last_served on a 503, this fails.
    db, clock = registry.db, registry.clock
    tarball, tsha, _ = _tarball()
    _seed_release(db, clock, "v0.0.2", tsha, len(tarball))  # not cached
    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        assert client.get(
            "/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_CY}
        ).status_code == 503
    with db.transaction() as conn:
        device = conn.execute(
            "SELECT last_served_tag, boot_outcome FROM devices WHERE device_id=%s",
            (device_id_for_serial(SERIAL_CY),),
        ).fetchone()
    # The row exists (idempotent upsert) but carries no boot record.
    assert device is not None
    assert device["last_served_tag"] is None and device["boot_outcome"] is None


def test_probe_base_health_conditional_guard_closes_lost_update(registry, tmp_path):
    # PROBE (remove the FOR UPDATE / conditional guard, E1): a base-health report
    # for a tag the device is NOT currently last-served (a concurrent recovery
    # serve moved last_served_tag) must NOT advance known-good. The write is a
    # single conditional UPDATE ... WHERE last_served_tag = running_tag; drop that
    # guard and the stale report wins the lost update.
    db, clock = registry.db, registry.clock
    tarball1, tsha1, _ = _tarball(SQUASHFS + b"one")
    tarball2, tsha2, _ = _tarball(SQUASHFS + b"two")
    _seed_release(db, clock, "v0.0.1", tsha1, len(tarball1))
    _seed_release(db, clock, "v0.0.2", tsha2, len(tarball2))
    device_id = device_id_for_serial(SERIAL_CY)
    with db.transaction() as conn:
        # The device was just rolled onto v0.0.1 (last_served=v0.0.1), while a
        # stale health report still claims v0.0.2.
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,last_served_tag,boot_outcome) "
            "VALUES(%s,%s,%s,'v0.0.1','pending')",
            (device_id, clock.utc(), clock.utc()),
        )
    token = "q" * 40
    _enroll(db, clock, device_id, token)
    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        response = client.post(
            "/v1/player/base-health",
            json={"authority_epoch": 1, "sequence": 1, "running_tag": "v0.0.2", "healthy": True},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 200 and response.json() == {"accepted": False}
    with db.transaction() as conn:
        device = conn.execute(
            "SELECT known_good_tag FROM devices WHERE device_id=%s", (device_id,)
        ).fetchone()
    assert device["known_good_tag"] is None  # the stale tag never advanced known-good


def test_probe_non_semver_tag_excluded_from_frontier_queries(registry, tmp_path):
    # PROBE (include a non-semver tag in latest-verified/discovered): a tag whose
    # PK CHECK passes but is not strict semver (v1.0.0.0) must be excluded from
    # BOTH queries, so a lower valid-semver tag wins. Include it and the query
    # names the wrong (higher-sorting-by-string) tag.
    db, clock = registry.db, registry.clock
    now = clock.utc()
    with db.transaction() as conn:
        _raw_release(conn, "v0.9.0", 0, 9, 0, now)       # valid semver, base facts
        _raw_release(conn, "v1.0.0.0", 1, 0, 0, now)     # NON-semver (4 segments)
        # latest-discovered ignores the non-semver tag.
        assert latest_discovered(conn) == "v0.9.0"
        # And so does latest-verified over known-good tags.
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,known_good_tag,known_good_at) "
            "VALUES('device-a',%s,%s,'v0.9.0',%s),('device-b',%s,%s,'v1.0.0.0',%s)",
            (now, now, now, now, now, now),
        )
        assert latest_verified(conn) == "v0.9.0"
