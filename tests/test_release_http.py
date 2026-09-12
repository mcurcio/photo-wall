"""HTTP composition of central boot, enrollment, time and artifact authority."""

import base64
import hashlib
import secrets
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from central.app import create_app
from central.releases import ReleaseAuthority
from contracts.enrollment import OutputReport, enrollment_message
from contracts.release import BootRequest, Release
from tests.conftest import BOOT_ABI

ADMIN = "release-http-operator-" + "x" * 32


def test_boot_enrollment_time_health_and_exact_artifact_share_central_authority(registry, tmp_path):
    release_key = Ed25519PrivateKey.generate()
    authority = ReleaseAuthority(
        registry.db, registry.clock, release_key.public_key(), BOOT_ABI
    )
    rootfs = b"central immutable root filesystem"
    release = Release(
        revision="e" * 40,
        boot_abi=BOOT_ABI,
        rootfs_sha256=hashlib.sha256(rootfs).hexdigest(),
        rootfs_size=len(rootfs),
    )
    manifest = release.encode()
    authority.register(manifest, release_key.sign(manifest))
    authority.set_default(release.release_id)
    (tmp_path / release.rootfs_name).write_bytes(rootfs)

    app = create_app(
        registry.db,
        registry.clock,
        ADMIN,
        release_authority=authority,
        release_root=tmp_path,
    )
    device_id = "device-" + "f" * 64
    boot_id = str(uuid.uuid4())
    boot_request = BootRequest(device_id, boot_id, secrets.token_hex(24))
    player_key = Ed25519PrivateKey.generate()
    public_key = player_key.public_key().public_bytes_raw().hex()

    with TestClient(app) as client:
        selected = client.post("/v1/bootstrap/boot", json=boot_request.__dict__)
        assert selected.status_code == 200
        ticket = selected.json()
        assert ticket["release_id"] == release.release_id
        assert selected.headers["cache-control"] == "no-store"

        image = client.get(f"/appliance/{release.rootfs_name}")
        assert image.status_code == 200 and image.content == rootfs
        assert image.headers["content-length"] == str(len(rootfs))

        nonce = client.post("/v1/enrollment/challenge", json={"public_key": public_key}).json()[
            "nonce"
        ]
        outputs = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),)
        signature = player_key.sign(
            enrollment_message(nonce, outputs, device_id, boot_id, ticket["ticket_id"])
        )
        enrollment = {
            "public_key": public_key,
            "nonce": nonce,
            "signature": base64.b64encode(signature).decode(),
            "outputs": [output.model_dump(mode="json") for output in outputs],
            "device_id": device_id,
            "boot_id": boot_id,
            "ticket_id": ticket["ticket_id"],
        }
        identity = client.post("/v1/enrollment/register", json=enrollment)
        assert identity.status_code == 200
        session = identity.json()
        headers = {"Authorization": "Bearer " + session["token"]}

        sampled = client.get("/v1/player/time", headers=headers)
        assert sampled.status_code == 200
        assert sampled.json() == {
            "protocol": 1,
            "player_id": session["player_id"],
            "authority_epoch": session["authority_epoch"],
            "server_time": registry.clock.utc(),
        }
        health = client.post(
            "/v1/player/boot-health",
            headers=headers,
            json={
                "ticket_id": ticket["ticket_id"],
                "healthy": True,
                "observed_at": registry.clock.utc(),
            },
        )
        assert health.status_code == 200
        assert health.json()["release_id"] == release.release_id


def test_release_artifact_rejects_wrong_sized_file(registry, tmp_path):
    release = registry.release_authority.release_for_rootfs("d" * 64)
    (tmp_path / release.rootfs_name).write_bytes(b"short")
    app = create_app(
        registry.db,
        registry.clock,
        ADMIN,
        release_authority=registry.release_authority,
        release_root=tmp_path,
    )
    with TestClient(app) as client:
        response = client.get(f"/appliance/{release.rootfs_name}")
        assert response.status_code == 503
        assert response.json() == {"error": "release_artifact_invalid"}
