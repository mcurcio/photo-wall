"""Player retirement advances the device lifecycle and refuses re-enrollment of a retired device."""

import base64
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial
from central.registry import (
    Enrollment,
    FrameCreate,
    OutputReport,
    RegistryError,
    enrollment_message,
)
from contracts.models import FrameProfile

SERIAL = "abcdef1234567890"
DEVICE_ID = device_id_for_serial(SERIAL)
assert DEVICE_ID is not None
PLAYER_ID = "p-lifecycle-test"
BOOT_ID = UUID(int=41)


def _seed_player(registry, *, canonical_device: bool) -> None:
    with registry.db.transaction() as conn:
        if canonical_device:
            conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                         "VALUES(%s,%s,900,900)", (DEVICE_ID, SERIAL))
        conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,"
                     "device_id) VALUES(%s,%s,%s,900,900,%s)",
                     (PLAYER_ID, "test-public-key", "test-token-hash", DEVICE_ID))


def test_ticketless_player_retirement_creates_canonical_tombstone_once(registry) -> None:
    _seed_player(registry, canonical_device=False)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT 1 FROM devices WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone() is None
    registry.retire(PLAYER_ID)
    registry.retire(PLAYER_ID)
    with registry.db.transaction() as conn:
        device = conn.execute("SELECT retired_at FROM devices WHERE device_id=%s",
                              (DEVICE_ID,)).fetchone()
        lifecycle = conn.execute("SELECT generation,revoked_at FROM fleet_device_lifecycle "
                                 "WHERE device_id=%s", (DEVICE_ID,)).fetchone()
        player = conn.execute("SELECT retired_at,authority_epoch FROM players WHERE id=%s",
                              (PLAYER_ID,)).fetchone()
    assert device["retired_at"] == 1000
    assert lifecycle == {"generation": 2, "revoked_at": 1000}
    assert player == {"retired_at": 1000, "authority_epoch": 2}


def test_retired_canonical_device_refuses_new_app_enrollment(registry) -> None:
    _seed_player(registry, canonical_device=True)
    registry.retire(PLAYER_ID)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    nonce = registry.challenge(public)["nonce"]
    outputs = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),)
    boot_id = str(BOOT_ID)
    signature = base64.b64encode(key.sign(enrollment_message(
        nonce, outputs, DEVICE_ID, boot_id, None))).decode()
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs,
                         device_id=DEVICE_ID, boot_id=boot_id, ticket_id=None,
                         signature=signature)
    with pytest.raises(RegistryError, match="retired_device"):
        registry.enroll(request)


def test_bound_retirement_refusal_does_not_create_a_device_tombstone(registry) -> None:
    _seed_player(registry, canonical_device=False)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO outputs(player_id,output_id,observation) VALUES(%s,%s,%s)",
                     (PLAYER_ID, "HDMI-A-1", Jsonb({"output_id": "HDMI-A-1",
                                                    "width_px": 1920, "height_px": 1080})))
    registry.create_frame(FrameCreate(
        id="lifecycle-frame", width_mm=300, height_mm=200,
        profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)))
    registry.bind("lifecycle-frame", PLAYER_ID, "HDMI-A-1", expected_generation=0)
    with pytest.raises(RegistryError, match="player_bound"):
        registry.retire(PLAYER_ID)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT 1 FROM devices WHERE device_id=%s",
                            (DEVICE_ID,)).fetchone() is None
        assert conn.execute("SELECT retired_at FROM players WHERE id=%s", (PLAYER_ID,)
                            ).fetchone()["retired_at"] is None
