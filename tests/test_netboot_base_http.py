"""HTTP composition of the 0009 Phase-4 netboot base route (central-only,
additive). Modelled on the .deb route's O_NOFOLLOW/fstat/streaming discipline
(see tests/test_app_package_http.py); unauthenticated on the trusted LAN."""

import base64
import hashlib
import os

from fastapi.testclient import TestClient

from central.app import create_app
from central.netboot_base import BASE_IMAGE_NAME, SERIAL_HEADER

ADMIN = "netboot-base-http-operator-" + "x" * 32
BODY = b"the rpi-image-gen base squashfs bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()


def _stage_base(root, *, body=BODY):
    (root / BASE_IMAGE_NAME).write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    (root / "SHA256SUMS").write_text(
        f"{'a' * 64}  ./boot/initrd.img\n{digest}  ./{BASE_IMAGE_NAME}\n"
    )


def test_serves_bytes_with_digest_and_length_reads_serial_unauthenticated(registry, tmp_path):
    _stage_base(tmp_path)
    app = create_app(registry.db, registry.clock, ADMIN, base_root=tmp_path)
    with TestClient(app) as client:
        # No Authorization header -- trusted-LAN boot path, like /v1/app/manifest.
        response = client.get("/v1/netboot/base", headers={SERIAL_HEADER: "10000000abcd1234"})
        assert response.status_code == 200
        assert response.content == BODY
        assert response.headers["content-length"] == str(len(BODY))
        expected = "sha-256=" + base64.b64encode(bytes.fromhex(SHA256)).decode()
        assert response.headers["digest"] == expected


def test_no_base_root_configured_is_503(registry):
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base")
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_unavailable"}


def test_missing_sha256sums_is_503(registry, tmp_path):
    (tmp_path / BASE_IMAGE_NAME).write_bytes(BODY)  # bytes present, no SHA256SUMS
    app = create_app(registry.db, registry.clock, ADMIN, base_root=tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base")
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_unavailable"}


def test_missing_squashfs_file_is_503(registry, tmp_path):
    (tmp_path / "SHA256SUMS").write_text(f"{SHA256}  ./{BASE_IMAGE_NAME}\n")  # sums only
    app = create_app(registry.db, registry.clock, ADMIN, base_root=tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base")
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_unavailable"}


def test_symlinked_base_is_refused(registry, tmp_path):
    outside = tmp_path.parent / "outside-base.squashfs"
    outside.write_bytes(b"outside-root secret bytes that must never be served")
    base_root = tmp_path / "base-root"
    base_root.mkdir()
    os.symlink(outside, base_root / BASE_IMAGE_NAME)
    (base_root / "SHA256SUMS").write_text(f"{SHA256}  ./{BASE_IMAGE_NAME}\n")

    app = create_app(registry.db, registry.clock, ADMIN, base_root=base_root)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base")
        assert response.status_code == 503
        assert response.json() == {"error": "base_artifact_unavailable"}


def test_serves_without_a_serial_header(registry, tmp_path):
    _stage_base(tmp_path)
    app = create_app(registry.db, registry.clock, ADMIN, base_root=tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/netboot/base")
        assert response.status_code == 200
        assert response.content == BODY
