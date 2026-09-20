"""HTTP composition of the 0012 per-device netboot base route (central + Postgres).

The pre-0012 single-staged-file model is gone: a serial now maps to a `devices`
row, resolves ONE tag, and the bytes are the per-version immutable
`base-<tag>.squashfs`, served with the `Digest` recorded on the tag's
`base_cache` row. Unauthenticated on the trusted LAN (0009), same O_NOFOLLOW /
fstat / bounded-stream discipline as the `.deb` route.
"""

import base64
import hashlib
import os

from fastapi.testclient import TestClient

from central.app import create_app
from central.app_release_queue import QueueReceipt
from central.app_releases import AppReleases
from central.netboot_base import SERIAL_HEADER, base_file_path, device_id_for_serial

ADMIN = "netboot-base-http-operator-" + "x" * 32
BODY = b"the rpi-image-gen base squashfs bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()
SERIAL = "10000000abcd1234"
TAG = "v1.2.3"


class _FakeQueue:
    def __init__(self):
        self.base_fetches = []

    def enqueue_base_fetch_in(self, conn, tag):
        self.base_fetches.append(tag)
        return QueueReceipt(coalesced=False)

    def enqueue_mirror_in(self, conn, tag):  # pragma: no cover
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
        base_tarball_size=len(BODY),
        base_tarball_url="https://example.test/base.tgz",
    )


def _seed_cached(registry, base_root, tag, *, body=BODY, pin_serial=SERIAL):
    """A release whose base is cached: the release row, a cached base_cache row,
    the per-version file, and a device pinned to the tag so the serial resolves
    deterministically to it."""
    base_root.mkdir(parents=True, exist_ok=True)
    base_file_path(base_root, tag).write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    db, clock = registry.db, registry.clock
    _seed_release(registry, tag)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO base_cache(tag,state,squashfs_sha256,size,updated_at) "
            "VALUES(%s,'cached',%s,%s,%s)",
            (tag, sha, len(body), clock.utc()),
        )
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,attached_tag) VALUES(%s,%s,%s,%s)",
            (device_id_for_serial(pin_serial), clock.utc(), clock.utc(), tag),
        )
    return sha


def _app(registry, base_root):
    return create_app(
        registry.db, registry.clock, ADMIN, base_root=base_root, release_queue=_FakeQueue()
    )


def test_serves_bytes_with_digest_and_length_reads_serial_unauthenticated(registry, tmp_path):
    _seed_cached(registry, tmp_path, TAG)
    with TestClient(_app(registry, tmp_path)) as client:
        # No Authorization header -- trusted-LAN boot path, like /v1/app/manifest.
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL})
        assert response.status_code == 200
        assert response.content == BODY
        assert response.headers["content-length"] == str(len(BODY))
        assert response.headers["digest"] == "sha-256=" + base64.b64encode(
            bytes.fromhex(SHA256)
        ).decode()


def test_records_last_served_on_the_200(registry, tmp_path):
    _seed_cached(registry, tmp_path, TAG)
    with TestClient(_app(registry, tmp_path)) as client:
        assert client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL}).status_code == 200
    with registry.db.transaction() as conn:
        device = conn.execute(
            "SELECT last_served_tag, boot_outcome FROM devices WHERE device_id=%s",
            (device_id_for_serial(SERIAL),),
        ).fetchone()
    assert device["last_served_tag"] == TAG and device["boot_outcome"] == "pending"


def test_no_base_root_configured_is_503(registry):
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base")
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_unavailable"}


def test_known_but_uncached_tag_is_503_uncached(registry, tmp_path):
    # A release is discovered (a pinned device resolves to it) but its bytes are
    # not yet cached: fail closed with the uncached code, not a crash.
    _seed_release(registry, TAG)
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,attached_tag) VALUES(%s,%s,%s,%s)",
            (device_id_for_serial(SERIAL), registry.clock.utc(), registry.clock.utc(), TAG),
        )
    with TestClient(_app(registry, tmp_path)) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL})
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_uncached"}


def test_symlinked_base_file_is_refused(registry, tmp_path):
    outside = tmp_path.parent / "outside-base.squashfs"
    outside.write_bytes(b"outside-root secret bytes that must never be served")
    base_root = tmp_path / "base-root"
    _seed_cached(registry, base_root, TAG)
    target = base_file_path(base_root, TAG)
    target.unlink()
    os.symlink(outside, target)  # O_NOFOLLOW must refuse this
    with TestClient(_app(registry, base_root)) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: SERIAL})
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_unavailable"}


def test_unsafe_serial_validated_at_the_seam_creates_no_row_and_serves_best_effort(
    registry, tmp_path
):
    # Seam-validation guarantee (folded here from the old no-DB unit test): a
    # raw/attacker-controlled serial reaching select_base_for_serial -- even
    # bypassing the route's own sanitize -- must NOT key a device lookup or create
    # a row. It is sanitized to None at the seam; with a live frontier the request
    # still resolves latest-verified and serves best-effort.
    sha = _seed_cached(registry, tmp_path, TAG)  # pins the SERIAL device (1 row)
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,first_seen,last_seen,known_good_tag,known_good_at) "
            "VALUES('device-frontier',%s,%s,%s,%s)",
            (registry.clock.utc(), registry.clock.utc(), TAG, registry.clock.utc()),
        )
        before = conn.execute("SELECT count(*) AS n FROM devices").fetchone()["n"]
    with TestClient(_app(registry, tmp_path)) as client:
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: "../../etc/passwd"})
        assert response.status_code == 200
        assert response.headers["digest"] == "sha-256=" + base64.b64encode(
            bytes.fromhex(sha)
        ).decode()
    with registry.db.transaction() as conn:
        after = conn.execute("SELECT count(*) AS n FROM devices").fetchone()["n"]
        # No new row keyed on the unsafe serial: the seam did not trust raw input.
        assert after == before
        assert conn.execute(
            "SELECT count(*) AS n FROM devices WHERE serial=%s", ("../../etc/passwd",)
        ).fetchone()["n"] == 0
