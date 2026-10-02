"""Device lifecycle revokes OS command state without promoting T0 claims."""

import base64
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial
from central.fleet.models import CheckIn
from central.fleet.service import FleetService
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
SESSION_ID = UUID(int=42)
ATTEMPT_ID = UUID(int=43)
OFFER_ID = UUID(int=44)
AUDIENCE = "test-installation-1"
KEY_SHA = "a" * 64


def _seed_player(registry, *, canonical_device: bool) -> None:
    with registry.db.transaction() as conn:
        if canonical_device:
            conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                         "VALUES(%s,%s,900,900)", (DEVICE_ID, SERIAL))
        conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,"
                     "device_id) VALUES(%s,%s,%s,900,900,%s)",
                     (PLAYER_ID, "test-public-key", "test-token-hash", DEVICE_ID))


def _seed_offer(conn, *, audience: str = AUDIENCE) -> None:
    conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                 "discovered_at,updated_at) VALUES('v1.0.0',1,0,0,FALSE,900,900)")
    conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,"
                 "serial,kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                 "app_policy_source,app_policy_revision,base_tag,base_content_key,"
                 "base_sha256,base_size,app_status,compatibility_basis,created_at,expires_at) "
                 "VALUES(%s,%s,%s,%s,%s,%s,'operator_baseline',0,'legacy_promotion',0,"
                 "'v1.0.0',%s,%s,100,'unconfigured','none',900,1100)",
                 (OFFER_ID, audience, DEVICE_ID, SERIAL, BOOT_ID, "b" * 32,
                  "b" * 64, "c" * 64))


def _seed_session(conn, session_id: UUID) -> None:
    conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                 "device_generation,kernel_boot_id,offer_id,installation_audience,trust_mode,"
                 "agent_key_sha256,verifier_ref,issued_at,expires_at) "
                 "VALUES(%s,%s,1,%s,%s,%s,'t1',%s,'test-gateway-session',900,1100)",
                 (session_id, DEVICE_ID, BOOT_ID, OFFER_ID, AUDIENCE, KEY_SHA))


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


def test_retirement_revokes_same_generation_session_and_queued_attempt(registry) -> None:
    _seed_player(registry, canonical_device=True)
    with registry.db.transaction() as conn:
        _seed_offer(conn)
        _seed_session(conn, SESSION_ID)
        conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                     "desired_revision,target_sha256,phase,created_at,updated_at,"
                     "device_generation) VALUES(%s,%s,%s,1,%s,'queued',900,900,1)",
                     (ATTEMPT_ID, DEVICE_ID, OFFER_ID, "d" * 64))
        assert conn.execute("SELECT count(*) AS n FROM "
                            "fleet_generation_current_app_attempts").fetchone()["n"] == 1
    registry.retire(PLAYER_ID)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM "
                            "fleet_generation_current_os_command_sessions").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM "
                            "fleet_generation_current_app_attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT revoked_at FROM fleet_os_command_sessions "
                            "WHERE command_session_id=%s", (SESSION_ID,)
                            ).fetchone()["revoked_at"] == 1000
        assert conn.execute("SELECT revoked_at FROM fleet_app_attempts "
                            "WHERE attempt_id=%s", (ATTEMPT_ID,)
                            ).fetchone()["revoked_at"] == 1000


def test_new_os_session_requires_explicit_revocation_of_previous_one(registry) -> None:
    _seed_player(registry, canonical_device=True)
    with registry.db.transaction() as conn:
        _seed_offer(conn)
        _seed_session(conn, SESSION_ID)
    with pytest.raises(UniqueViolation):
        with registry.db.transaction() as conn:
            _seed_session(conn, UUID(int=45))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=1000 "
                     "WHERE command_session_id=%s", (SESSION_ID,))
        _seed_session(conn, UUID(int=45))


def test_retired_device_still_accepts_bounded_t0_observation_only(registry) -> None:
    _seed_player(registry, canonical_device=True)
    registry.retire(PLAYER_ID)
    receipt = FleetService(registry.db, registry.clock).record_check_in(
        CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=BOOT_ID,
                agent_incarnation="test-agent", observation_sequence=1,
                phase="base_ready"))
    assert receipt == {"accepted": True}
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_command_sessions"
                            ).fetchone()["n"] == 0


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
