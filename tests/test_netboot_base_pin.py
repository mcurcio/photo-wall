"""0012 bead 7: the operator attachment surface (admin-token-gated pin).

Exercised against a REAL Postgres schema (the `registry` fixture, which skips
without PHOTO_WALL_TEST_DATABASE_URL) through the composed FastAPI app, so the
admin auth dependency, the `devices`/`app_releases`/`base_cache` schema, and the
serve-time precedence resolver are all real; only the enqueue port is a faithful
fake (returns a real `QueueReceipt`, never None/Mock).

Covers the bead-7 acceptance criteria and their mutation probes:
  (a) set pin=T  => serve resolves to T (pin wins over latest-verified);
      a fetch_base(T) is enqueued          [PROBE: drop the proactive fetch]
  (b) clear pin  => serve falls back to latest-verified; post-clear GC runs
  (c) unauthorized (missing/invalid token) => 401, NO mutation
                                              [PROBE: drop the admin dep]
  (d) set to a non-existent tag => 4xx, NO mutation (FK/validation)
  (e) post-attachment GC (E5): after a set, the previously-pinned tag -- now
      unreferenced -- is evicted           [PROBE: drop the post-attachment GC]
"""

from __future__ import annotations

import base64
import hashlib

from fastapi.testclient import TestClient

from central.app import create_app
from central.app_packages import AppPackages
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.netboot_base import SERIAL_HEADER, base_file_path, device_id_for_serial

ADMIN = "netboot-base-pin-operator-" + "x" * 32
SERIAL = "10000000abcd1234"
DEVICE_ID = device_id_for_serial(SERIAL)
T_LOW = "v1.0.0"    # the pinned (lower-semver) tag -- proves a pin beats the frontier
T_HIGH = "v2.0.0"   # a higher-semver latest-verified frontier held by another device
T_OTHER = "v1.5.0"  # a re-pin target for the (e) eviction probe


class _FakeQueue:
    """A faithful enqueue fake: records the fetched/mirrored tags and returns a
    real QueueReceipt (never None/Mock), matching AppReleaseTaskQueue's contract."""

    def __init__(self):
        self.base_fetches: list[str] = []
        self.mirrors: list[str] = []

    def enqueue_base_fetch_in(self, conn, tag):
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag):
        self.mirrors.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_poll_in(self, conn):  # pragma: no cover
        return QueueReceipt(coalesced=False)


def _seed_release(registry, tag):
    AppReleases(registry.db, registry.clock).upsert_discovered(
        tag,
        asset_sha256="a" * 64,
        asset_size=10,
        asset_url="https://example.test/app.deb",
        base_tarball_sha256="b" * 64,
        base_tarball_size=64,
        base_tarball_url="https://example.test/base.tgz",
    )


def _cache_bytes(registry, base_root, tag, body):
    """A cached base: the per-version file plus a 'cached' base_cache row."""
    base_root.mkdir(parents=True, exist_ok=True)
    base_file_path(base_root, tag).write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO base_cache(tag,state,squashfs_sha256,size,updated_at) "
            "VALUES(%s,'cached',%s,%s,%s)",
            (tag, sha, len(body), registry.clock.utc()),
        )
    return sha


def _insert_device(registry, device_id, **cols):
    cols.setdefault("first_seen", 1000.0)
    cols.setdefault("last_seen", 1000.0)
    names = ["device_id", *cols]
    placeholders = ",".join(["%s"] * len(names))
    with registry.db.transaction() as conn:
        conn.execute(
            f"INSERT INTO devices({','.join(names)}) VALUES({placeholders})",
            (device_id, *cols.values()),
        )


def _device_row(registry, device_id):
    with registry.db.transaction() as conn:
        return conn.execute(
            "SELECT attached_tag FROM devices WHERE device_id=%s", (device_id,)
        ).fetchone()


def _cache_state(registry, tag):
    with registry.db.transaction() as conn:
        return conn.execute(
            "SELECT state, eviction_reason FROM base_cache WHERE tag=%s", (tag,)
        ).fetchone()


def _app(registry, base_root, queue):
    return create_app(
        registry.db, registry.clock, ADMIN, base_root=base_root, release_queue=queue
    )


def _auth(token=ADMIN):
    return {"Authorization": f"Bearer {token}"}


# -- (a) set pin: serve resolves to the pin + a fetch is enqueued -------------


def test_a_set_pin_serve_resolves_to_pin_over_frontier_and_enqueues_fetch(registry, tmp_path):
    # Frontier: another non-retired device is known-good on the HIGHER tag, so
    # latest-verified = T_HIGH. Our device is unpinned to start.
    _seed_release(registry, T_LOW)
    _seed_release(registry, T_HIGH)
    low_sha = _cache_bytes(registry, tmp_path, T_LOW, b"low base bytes")
    _insert_device(registry, "device-frontier", known_good_tag=T_HIGH, known_good_at=1000.0)
    _insert_device(registry, DEVICE_ID)
    queue = _FakeQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        put = client.put(f"/v1/operator/devices/{DEVICE_ID}/pin", json={"tag": T_LOW}, headers=_auth())
        assert put.status_code == 200

        # PROBE (drop the proactive base fetch on set): the pinned tag is enqueued
        # so the device's next netboot hits cached bytes, not a 503. Skipping the
        # enqueue reds this line -- criterion (a)'s base-fetch assertion.
        assert queue.base_fetches == [T_LOW]

        # PROBE (drop the proactive .deb mirror on set): T_LOW is deployable but
        # not yet mirrored (_seed_release leaves mirrored_sha256 NULL), so the
        # design-gate proactive trigger also pre-warms its `.deb` -- otherwise a
        # RECOVERY pin pays an extra netboot -> manifest-503 -> reboot cycle.
        # Skipping enqueue_mirror_in reds this line.
        assert queue.mirrors == [T_LOW]

        # Pin WINS over latest-verified: even though T_HIGH is a higher-semver
        # verified frontier, the serve seam resolves the device to its pin T_LOW.
        served = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL})
        assert served.status_code == 200
        assert served.headers["digest"] == "sha-256=" + base64.b64encode(
            bytes.fromhex(low_sha)
        ).decode()

    assert _device_row(registry, DEVICE_ID)["attached_tag"] == T_LOW


def test_a_set_pin_does_not_enqueue_a_redundant_mirror_when_already_mirrored(registry, tmp_path):
    # The pinned tag's `.deb` is ALREADY mirrored, so the warranted-guard
    # (deb_mirrorable_in) is False and no redundant mirror is enqueued -- but the
    # base fetch still fires (idempotent/coalesced). Proves the guard, not just
    # the happy path.
    _seed_release(registry, T_LOW)
    # Link the `.deb` bytes: register them in app_packages (the mirrored_sha256 FK
    # target) then mark the release mirrored, so deb_mirrorable_in is False.
    deb_sha = hashlib.sha256(b"deb bytes for " + T_LOW.encode()).hexdigest()
    releases = AppReleases(registry.db, registry.clock)
    AppPackages(registry.db, registry.clock).register(version=T_LOW, sha256=deb_sha, size=10)
    releases.mark_mirrored(T_LOW, deb_sha)
    _insert_device(registry, DEVICE_ID)
    queue = _FakeQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        put = client.put(f"/v1/operator/devices/{DEVICE_ID}/pin", json={"tag": T_LOW}, headers=_auth())
        assert put.status_code == 200

    assert queue.base_fetches == [T_LOW]
    # No redundant mirror for an already-mirrored tag.
    assert queue.mirrors == []


# -- (b) clear pin: fall back to latest-verified + post-clear GC --------------


def test_b_clear_pin_falls_back_to_latest_verified_and_gc_reclaims_bytes(registry, tmp_path):
    _seed_release(registry, T_LOW)
    _seed_release(registry, T_HIGH)
    _cache_bytes(registry, tmp_path, T_LOW, b"low base bytes")
    high_sha = _cache_bytes(registry, tmp_path, T_HIGH, b"high base bytes")
    # Frontier device holds T_HIGH known-good; our device is pinned to T_LOW,
    # which nothing else references (so it becomes evictable once the pin clears).
    _insert_device(registry, "device-frontier", known_good_tag=T_HIGH, known_good_at=1000.0)
    _insert_device(registry, DEVICE_ID, attached_tag=T_LOW)

    with TestClient(_app(registry, tmp_path, _FakeQueue())) as client:
        cleared = client.delete(f"/v1/operator/devices/{DEVICE_ID}/pin", headers=_auth())
        assert cleared.status_code == 200
        assert _device_row(registry, DEVICE_ID)["attached_tag"] is None

        # Fallback: with the pin gone, the device resolves latest-verified T_HIGH.
        served = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL})
        assert served.status_code == 200
        assert served.headers["digest"] == "sha-256=" + base64.b64encode(
            bytes.fromhex(high_sha)
        ).decode()

    # Post-clear GC (E5): T_LOW is no longer any pin/known-good and is not
    # latest-verified, so its bytes are reclaimed by the clear -- not left for the
    # next poll tail. T_HIGH (latest-verified) is kept.
    assert _cache_state(registry, T_LOW)["state"] == "evicted"
    assert not base_file_path(tmp_path, T_LOW).exists()
    assert _cache_state(registry, T_HIGH)["state"] == "cached"


# -- (c) unauthorized: 401, no mutation ---------------------------------------


def test_c_unauthorized_set_and_clear_are_rejected_with_no_mutation(registry, tmp_path):
    _seed_release(registry, T_LOW)
    _insert_device(registry, DEVICE_ID)
    queue = _FakeQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        # PROBE (drop the admin dependency): with no/invalid token the mutation
        # must be refused. Removing Depends(admin) would 200 these and red the
        # status assertions AND the no-mutation assertions below.
        missing = client.put(f"/v1/operator/devices/{DEVICE_ID}/pin", json={"tag": T_LOW})
        assert missing.status_code == 401
        invalid = client.put(
            f"/v1/operator/devices/{DEVICE_ID}/pin",
            json={"tag": T_LOW},
            headers=_auth("not-the-admin-token-" + "z" * 32),
        )
        assert invalid.status_code == 401
        unpin = client.delete(f"/v1/operator/devices/{DEVICE_ID}/pin", headers=_auth("wrong"))
        assert unpin.status_code == 401

    # No mutation happened and nothing was enqueued off an unauthenticated call.
    assert _device_row(registry, DEVICE_ID)["attached_tag"] is None
    assert queue.base_fetches == [] and queue.mirrors == []


# -- (d) non-existent tag: 4xx, no mutation -----------------------------------


def test_d_set_to_nonexistent_tag_is_rejected_with_no_mutation(registry, tmp_path):
    # The device exists and starts unpinned; the target tag was never discovered.
    _insert_device(registry, DEVICE_ID)
    queue = _FakeQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.put(
            f"/v1/operator/devices/{DEVICE_ID}/pin", json={"tag": "v9.9.9"}, headers=_auth()
        )
        # FK/validation: a tag absent from app_releases is a clean 4xx (404
        # release_not_found via the pre-write existence check), never an FK 500.
        assert response.status_code == 404
        assert response.json() == {"error": "release_not_found"}

    # Rejected BEFORE any mutation: the pin is untouched and nothing was enqueued.
    assert _device_row(registry, DEVICE_ID)["attached_tag"] is None
    assert queue.base_fetches == [] and queue.mirrors == []


def test_d_pin_unknown_device_is_404_with_no_release_side_effect(registry, tmp_path):
    # A valid tag but no device row (rows are auto-created only at the netboot
    # seam): 404 device_not_found, and -- because the pin write matched nothing --
    # neither the proactive fetch nor GC should have any device to act on.
    _seed_release(registry, T_LOW)
    queue = _FakeQueue()

    with TestClient(_app(registry, tmp_path, queue)) as client:
        response = client.put(
            "/v1/operator/devices/device-does-not-exist/pin",
            json={"tag": T_LOW},
            headers=_auth(),
        )
        assert response.status_code == 404
        assert response.json() == {"error": "device_not_found"}


# -- (e) post-attachment GC on a re-pin ---------------------------------------


def test_e_repin_evicts_the_previously_pinned_unreferenced_tag(registry, tmp_path):
    # Our device is pinned to T_LOW (cached). Nothing else references T_LOW.
    _seed_release(registry, T_LOW)
    _seed_release(registry, T_OTHER)
    _cache_bytes(registry, tmp_path, T_LOW, b"low base bytes")
    _insert_device(registry, DEVICE_ID, attached_tag=T_LOW)

    with TestClient(_app(registry, tmp_path, _FakeQueue())) as client:
        put = client.put(
            f"/v1/operator/devices/{DEVICE_ID}/pin", json={"tag": T_OTHER}, headers=_auth()
        )
        assert put.status_code == 200

    # PROBE (drop the post-attachment GC): re-pinning to T_OTHER leaves T_LOW
    # referenced by nothing (not a pin, not known-good, not latest-verified), so
    # its bytes are reclaimed promptly by the set. Skipping gc_base_cache leaves
    # T_LOW 'cached' with its file on disk and reds these lines -- criterion (e).
    assert _cache_state(registry, T_LOW)["state"] == "evicted"
    assert _cache_state(registry, T_LOW)["eviction_reason"] is not None
    assert not base_file_path(tmp_path, T_LOW).exists()
    assert _device_row(registry, DEVICE_ID)["attached_tag"] == T_OTHER
