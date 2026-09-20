"""0012 bead 2: server-side rollback + sticky recovery, end to end.

Exercised against a REAL Postgres schema (the `registry` fixture, which skips
without PHOTO_WALL_TEST_DATABASE_URL); only the GitHub network boundary is mocked
(via `httpx.MockTransport` serving a real base tarball). No mock of our own code.

The failing-boot detection, recovery-aware resolution, release, and poll-tail
sweep are the read side of the netboot seam (`select_base_for_serial` /
`sweep_failed_boots`). Tags are semver so the frontier queries order them:
  T1 = v0.0.1 (a device's prior known-good / rollback target)
  T  = v0.0.2 (a target that is 200-served but never reports healthy -> failed)
  T2 = v0.0.3 (a newer latest-verified that releases the stick)

Acceptance criteria (a-f) and three mutation probes each carry a comment naming
the mutation they would catch, so the suite is not theater.
"""

import asyncio
import hashlib
import io
import tarfile

import httpx
from fastapi.testclient import TestClient

from central.app import create_app
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.github_releases import GithubReleaseSource
from central.netboot_base import (
    PENDING_HEALTH_TIMEOUT,
    SERIAL_HEADER,
    device_id_for_serial,
    fetch_base,
    sweep_failed_boots,
)

ADMIN = "netboot-base-recovery-operator-" + "x" * 32
T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
SQUASHFS = {tag: (b"base squashfs for " + tag.encode()) * 8 for tag in (T1, T, T2)}
SERIAL_D = "10000000abcd0010"
BASE_URL = "https://example.test/download/photo-wall-base.tar.gz"


class _FakeQueue:
    def __init__(self):
        self.base_fetches = []

    def enqueue_base_fetch_in(self, conn, tag):
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag):  # pragma: no cover - unused here
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn):  # pragma: no cover - unused here
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
        return httpx.Response(
            200, content=tarball, headers={"Content-Length": str(len(tarball))}
        )

    return GithubReleaseSource("owner/repo", transport=httpx.MockTransport(handle))


def _seed_release(db, clock, tag):
    """Discover `tag` with base facts (no bytes cached yet)."""
    tarball, tsha = _tarball(SQUASHFS[tag])
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


def _cache_tag(db, clock, tag, base_root):
    """Seed + actually fetch (cache) `tag`'s bytes so a serve returns 200."""
    tarball = _seed_release(db, clock, tag)

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
        # Column names are test-controlled literals, never request input.
        conn.execute(
            f"INSERT INTO devices({','.join(names)}) VALUES({placeholders})",
            (device_id, *cols.values()),
        )


def _device(db, device_id):
    with db.transaction() as conn:
        return conn.execute(
            "SELECT last_served_tag, boot_outcome, failed_tag, known_good_tag "
            "FROM devices WHERE device_id=%s",
            (device_id,),
        ).fetchone()


def _enroll(db, clock, device_id, token, *, epoch=1):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    player_id = "p-" + hashlib.sha256(device_id.encode()).hexdigest()[:32]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO players(id,public_key,token_hash,authority_epoch,registered_at,"
            "last_seen,device_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (player_id, "pk-" + device_id, token_hash, epoch, clock.utc(), clock.utc(), device_id),
        )


def _frontier_device(db, clock, tag, name="device-frontier"):
    """A non-retired device already known-good on `tag`, so latest_verified==tag."""
    _insert_device(db, name, known_good_tag=tag, known_good_at=clock.utc())


# -- Acceptance criteria -----------------------------------------------------


def test_a_re_netboot_while_pending_detects_a_failed_boot(registry, tmp_path):
    # (a) A device 200-served T that re-netboots while still `pending` never
    # reached healthy: failed_tag=T, boot_outcome=failed. Here the device has no
    # known-good to fall back to and T is not currently cached, so the failed
    # state persists (no fresh 200 resets it) -- the pure detection observation.
    db, clock = registry.db, registry.clock
    _seed_release(db, clock, T)                      # discovered, NOT cached
    _frontier_device(db, clock, T)                   # latest_verified == T
    device_id = device_id_for_serial(SERIAL_D)
    _insert_device(db, device_id, last_served_tag=T, boot_outcome="pending",
                   last_served_at=1000.0)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        assert client.get(
            "/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_D}
        ).status_code == 503                         # T uncached -> fail closed
    row = _device(db, device_id)
    assert row["failed_tag"] == T and row["boot_outcome"] == "failed"


def test_b_recovery_serves_known_good_and_keeps_the_stick(registry, tmp_path):
    # (b) A device with known_good=T1 whose desired target T just failed is served
    # T1 (recovery); last_served_tag is recorded truthfully as T1 and failed_tag
    # stays T (the stick holds).
    db, clock = registry.db, registry.clock
    _cache_tag(db, clock, T1, tmp_path)              # rollback target, cached
    _seed_release(db, clock, T)                      # failing target
    _frontier_device(db, clock, T)                   # latest_verified == T
    device_id = device_id_for_serial(SERIAL_D)
    _insert_device(db, device_id, known_good_tag=T1, known_good_at=1000.0,
                   last_served_tag=T, boot_outcome="pending", last_served_at=1000.0)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_D})
    assert response.status_code == 200 and response.content == SQUASHFS[T1]
    row = _device(db, device_id)
    assert row["last_served_tag"] == T1              # truthful recovery record
    assert row["failed_tag"] == T                    # stick intact


def test_c_recovery_boot_confirms_itself_healthy_stick_holds(registry, tmp_path):
    # (c) The recovery boot posts base-health on T1: T1 == last_served_tag so it
    # validates, known_good is reconfirmed as T1, boot_outcome=healthy, and
    # failed_tag is NOT cleared (T1 != T) -- the stick still holds.
    db, clock = registry.db, registry.clock
    _seed_release(db, clock, T1)
    _seed_release(db, clock, T)
    device_id = device_id_for_serial(SERIAL_D)
    # Post-recovery state: served + running T1, still fenced off T.
    _insert_device(db, device_id, known_good_tag=T1, known_good_at=1000.0,
                   last_served_tag=T1, boot_outcome="pending", failed_tag=T,
                   last_served_at=1000.0)
    token = "z" * 40
    _enroll(db, clock, device_id, token)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        health = client.post(
            "/v1/player/base-health",
            json={"authority_epoch": 1, "sequence": 1, "running_tag": T1, "healthy": True},
            headers={"Authorization": "Bearer " + token},
        )
    assert health.status_code == 200 and health.json() == {"accepted": True}
    row = _device(db, device_id)
    assert row["known_good_tag"] == T1 and row["boot_outcome"] == "healthy"
    assert row["failed_tag"] == T                    # T1 != T -> stick NOT cleared


def test_d_repeated_reboots_keep_serving_known_good_no_oscillation(registry, tmp_path):
    # (d) While the desired target is still the fenced T, every reboot serves T1
    # and never oscillates back to T. T is cached too, so a broken recovery arm
    # would visibly serve T's bytes instead.
    db, clock = registry.db, registry.clock
    _cache_tag(db, clock, T1, tmp_path)
    _cache_tag(db, clock, T, tmp_path)               # cached -> a regression would serve it
    _frontier_device(db, clock, T)                   # latest_verified == T (fenced)
    device_id = device_id_for_serial(SERIAL_D)
    _insert_device(db, device_id, known_good_tag=T1, known_good_at=1000.0,
                   last_served_tag=T1, boot_outcome="pending", failed_tag=T,
                   last_served_at=1000.0)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        for _ in range(3):
            # PROBE (remove the recovery arm): served would fall through to the
            # fenced desired T and this content assertion would fail.
            response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_D})
            assert response.status_code == 200 and response.content == SQUASHFS[T1]
    row = _device(db, device_id)
    assert row["last_served_tag"] == T1 and row["failed_tag"] == T


def test_e_newer_latest_verified_releases_the_stick(registry, tmp_path):
    # (e) When a newer latest-verified T2 (> the fenced T) becomes the desired
    # target, the stick is released (failed_tag cleared) and one fresh `pending`
    # attempt is taken on T2.
    db, clock = registry.db, registry.clock
    _cache_tag(db, clock, T2, tmp_path)              # the new target, cached
    _seed_release(db, clock, T)
    _seed_release(db, clock, T1)
    _frontier_device(db, clock, T2)                  # latest_verified == T2 (> T)
    device_id = device_id_for_serial(SERIAL_D)
    _insert_device(db, device_id, known_good_tag=T1, known_good_at=1000.0,
                   last_served_tag=T1, boot_outcome="pending", failed_tag=T,
                   last_served_at=1000.0)

    app = create_app(db, clock, ADMIN, base_root=tmp_path, release_queue=_FakeQueue())
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL_D})
    assert response.status_code == 200 and response.content == SQUASHFS[T2]
    row = _device(db, device_id)
    # PROBE (skip clearing failed_tag on release): failed_tag would stay T and the
    # device would be perpetually mis-handled; this assertion catches it.
    assert row["failed_tag"] is None
    assert row["last_served_tag"] == T2 and row["boot_outcome"] == "pending"


def test_f_sweep_fails_stale_pending_and_leaves_a_live_stick_untouched(registry, tmp_path):
    # (f) The poll-tail sweep marks stale-`pending` devices failed. A device that
    # already carries a live failed_tag keeps it (E2 NULL-guard); a NULL-failed_tag
    # device is fenced to the tag it ACTUALLY SERVED (E2b), NOT a recomputed
    # frontier that has drifted higher; a device that served nothing gets no bogus
    # fence; a recently-served device and a retired device are left alone.
    db, clock = registry.db, registry.clock
    _seed_release(db, clock, T)
    _seed_release(db, clock, T1)
    _seed_release(db, clock, T2)
    _frontier_device(db, clock, T2)                  # latest_verified == T2 (!= T)
    threshold = clock.utc() - PENDING_HEALTH_TIMEOUT  # last_served_at < this => stale
    old, recent = threshold - 1, threshold + 1

    _insert_device(db, "device-stick", boot_outcome="pending", failed_tag=T,
                   last_served_tag=T1, last_served_at=old)          # live stick
    _insert_device(db, "device-served-lower", boot_outcome="pending",
                   last_served_tag=T, last_served_at=old)           # served T, frontier drifted to T2
    _insert_device(db, "device-never", boot_outcome="pending",
                   last_served_tag=None, last_served_at=old)        # never served / no pin
    _insert_device(db, "device-recent", boot_outcome="pending",
                   last_served_tag=T2, last_served_at=recent)       # within window
    _insert_device(db, "device-retired", boot_outcome="pending", failed_tag=None,
                   last_served_tag=T2, last_served_at=old, retired_at=1000.0)

    with db.transaction() as conn:
        swept = sweep_failed_boots(conn, clock=clock)
    assert swept == 3                                # stick + served-lower + never

    stick = _device(db, "device-stick")
    # PROBE (remove the E2 NULL-guard): the sweep would overwrite the live stick;
    # this must stay T.
    assert stick["failed_tag"] == T and stick["boot_outcome"] == "failed"

    # E2b regression: fence the SERVED tag T, never the drifted frontier T2 --
    # else the device would RECOVER next boot and silently skip a verified upgrade.
    served_lower = _device(db, "device-served-lower")
    assert served_lower["failed_tag"] == T and served_lower["boot_outcome"] == "failed"

    # Served nothing + no pin: failed outcome, but NO bogus fence.
    never = _device(db, "device-never")
    assert never["failed_tag"] is None and never["boot_outcome"] == "failed"

    assert _device(db, "device-recent")["boot_outcome"] == "pending"
    assert _device(db, "device-retired")["boot_outcome"] == "pending"
