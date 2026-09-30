"""PostgreSQL gates for immutable, exact-byte queued app attempts."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation

from central.content_catalog.catalog import device_id_for_serial
from central.fleet.attempts import AttemptService
from central.fleet.models import FleetError
from central.fleet.principal import PrincipalError, VerifiedOsPrincipal
from central.fleet.service import FleetService

SERIAL = "abcdef1234567890"
DEVICE_ID = device_id_for_serial(SERIAL)
assert DEVICE_ID is not None
BOOT_ID = UUID(int=901)
OFFER_ID = UUID(int=902)
SESSION_ID = UUID(int=903)
AUDIENCE = "test-installation-1"
BASE_TAG = "v1.0.0"
TARGET_TAG = "v1.0.1"
BASE_SHA = "a" * 64
BASE_TARBALL_SHA = "b" * 64
BASE_ABI = "sha256:" + "c" * 64
TARGET_SHA = "d" * 64
FALLBACK_SHA = "e" * 64


def _principal() -> VerifiedOsPrincipal:
    return VerifiedOsPrincipal(
        device_id=DEVICE_ID, device_generation=1, kernel_boot_id=BOOT_ID,
        offer_id=OFFER_ID, installation_audience=AUDIENCE, trust_mode="t1",
        command_session_id=SESSION_ID, agent_key_sha256="f" * 64,
        expires_at=1100,
    )


def _seed(registry, *, offer_audience: str = AUDIENCE,
          fallback_qualified: bool = True) -> None:
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,base_tarball_sha256,base_abi,"
                     "base_abi_squashfs_sha256,base_abi_source_manifest) "
                     "VALUES(%s,1,0,0,FALSE,900,900,%s,%s,%s,'manifest.v2.json')",
                     (BASE_TAG, BASE_TARBALL_SHA, BASE_ABI, BASE_SHA))
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,mirror_state,payload_url,payload_sha256,"
                     "payload_size,payload_format,payload_base_abi,payload_source_manifest) "
                     "VALUES(%s,1,0,1,FALSE,900,900,'mirrored',%s,%s,123,"
                     "'pw-player-data-v1',%s,'manifest.v2.json')",
                     (TARGET_TAG, "https://example.invalid/target-old.tar.gz",
                      TARGET_SHA, BASE_ABI))
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,900,900)", (DEVICE_ID, SERIAL))
        conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,"
                     "serial,kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                     "app_policy_source,app_policy_revision,base_tag,base_content_key,"
                     "base_sha256,base_size,app_status,compatibility_basis,offer_schema,"
                     "created_at,expires_at) "
                     "VALUES(%s,%s,%s,%s,%s,%s,'operator_baseline',1,'explicit',1,%s,%s,"
                     "%s,1024,'unconfigured','none',2,900,2000)",
                     (OFFER_ID, offer_audience, DEVICE_ID, SERIAL, BOOT_ID, "1" * 32,
                      BASE_TAG, BASE_TARBALL_SHA, BASE_SHA))
        conn.execute("INSERT INTO fleet_os_command_sessions(command_session_id,device_id,"
                     "device_generation,kernel_boot_id,offer_id,installation_audience,"
                     "trust_mode,agent_key_sha256,verifier_ref,issued_at,expires_at) "
                     "VALUES(%s,%s,1,%s,%s,%s,'t1',%s,'test-gateway',900,1100)",
                     (SESSION_ID, DEVICE_ID, BOOT_ID, OFFER_ID, AUDIENCE, "f" * 64))
        conn.execute("INSERT INTO fleet_app_policy(singleton,revision,target_tag,"
                     "target_sha256,target_size,target_format,changed_at) "
                     "VALUES(TRUE,1,%s,%s,123,'pw-player-data-v1',900)",
                     (TARGET_TAG, TARGET_SHA))
        if fallback_qualified:
            conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                         "sha256,size,base_abi,trust_mode,evidence_ref,accepted_at) "
                         "VALUES(%s,'app',%s,%s,80,%s,'t1','qualified-output-proof',900)",
                         (DEVICE_ID, FALLBACK_SHA, FALLBACK_SHA, BASE_ABI))
            conn.execute("INSERT INTO assets(kind,identity,created_at) "
                         "VALUES('player-payload',%s,900)", (FALLBACK_SHA,))
            conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                         "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                         "VALUES('player-payload',%s,%s,%s,%s,80,%s,80,900)",
                         (FALLBACK_SHA, f"fleet-fallback:{DEVICE_ID}",
                          "https://example.invalid/fallback.tar.gz", FALLBACK_SHA,
                          FALLBACK_SHA))


def _create(registry):
    return AttemptService(registry.db, registry.clock).create_queued(
        _principal(), desired_revision=1, expected_target_sha256=TARGET_SHA,
        fallback_sha256=FALLBACK_SHA)


def test_queued_attempt_freezes_two_exact_roots_and_survives_release_recut(registry) -> None:
    _seed(registry)
    first = _create(registry)
    assert first.phase == "queued" and first.root_released_at is None
    assert first.device_generation == 1 and first.base_abi == BASE_ABI
    assert {asset.sha256 for asset in first.assets} == {TARGET_SHA, FALLBACK_SHA}
    with registry.db.transaction() as conn:
        refs = conn.execute("SELECT identity,locator_url,locator_sha256,locator_size,"
                            "expected_sha256,expected_size FROM asset_references "
                            "WHERE owner=%s ORDER BY identity",
                            (f"fleet-attempt:{first.attempt_id}",)).fetchall()
        assert len(refs) == 2
        assert refs[0] == {"identity": TARGET_SHA,
                           "locator_url": "https://example.invalid/target-old.tar.gz",
                           "locator_sha256": TARGET_SHA, "locator_size": 123,
                           "expected_sha256": TARGET_SHA, "expected_size": 123}
        assert refs[1]["identity"] == FALLBACK_SHA
        assert refs[1]["locator_url"] == "https://example.invalid/fallback.tar.gz"
        conn.execute("UPDATE app_releases SET payload_url=%s,payload_sha256=%s "
                     "WHERE tag=%s", ("https://example.invalid/recut.tar.gz",
                                      "0" * 64, TARGET_TAG))
    assert _create(registry) == first
    assert not FleetService(registry.db, registry.clock).evict_if_unretained(
        kind="app", content_key=TARGET_SHA, evict=lambda: pytest.fail("evicted active target"))
    with pytest.raises(FleetError, match="attempt_retry_conflict"):
        AttemptService(registry.db, registry.clock).create_queued(
            _principal(), desired_revision=1, expected_target_sha256=TARGET_SHA,
            fallback_sha256="1" * 64)


def test_missing_qualified_fallback_does_not_publish_attempt_or_root(registry) -> None:
    _seed(registry, fallback_qualified=False)
    with pytest.raises(FleetError, match="attempt_fallback_unavailable"):
        _create(registry)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM asset_references "
                            "WHERE owner LIKE 'fleet-attempt:%'").fetchone()["n"] == 0


def test_t0_offer_cannot_publish_attempt(registry) -> None:
    _seed(registry, offer_audience="photo-wall-central-t0")
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        _create(registry)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 0


def test_snapshot_and_locator_cannot_change_and_legacy_cannot_upgrade(registry) -> None:
    _seed(registry)
    attempt = _create(registry)
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_app_attempts SET target_size=124 WHERE attempt_id=%s",
                         (attempt.attempt_id,))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("DELETE FROM fleet_app_attempts WHERE attempt_id=%s",
                         (attempt.attempt_id,))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE asset_references SET locator_url=%s WHERE owner=%s",
                         ("https://example.invalid/altered.tar.gz",
                          f"fleet-attempt:{attempt.attempt_id}"))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("DELETE FROM asset_references WHERE owner=%s",
                         (f"fleet-attempt:{attempt.attempt_id}",))
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                     "desired_revision,target_sha256,phase,created_at,updated_at) "
                     "VALUES(%s,%s,%s,2,%s,'queued',900,900)",
                     (UUID(int=904), DEVICE_ID, OFFER_ID, "2" * 64))
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_app_attempts SET attempt_schema=1 "
                         "WHERE attempt_id=%s", (UUID(int=904),))


def test_two_retries_share_one_attempt_id(registry) -> None:
    _seed(registry)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_create, registry)
        second = pool.submit(_create, registry)
        assert first.result(timeout=20).attempt_id == second.result(timeout=20).attempt_id
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 1


def test_session_generation_change_blocks_new_attempt(registry) -> None:
    _seed(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_device_lifecycle SET generation=2 WHERE device_id=%s",
                     (DEVICE_ID,))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        _create(registry)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 0


def test_retry_rejects_changed_policy_and_live_release(registry) -> None:
    _seed(registry)
    attempt = _create(registry)
    service = AttemptService(registry.db, registry.clock)
    with pytest.raises(FleetError, match="attempt_release_requires_revocation"):
        service.release_queued(attempt.attempt_id)
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_app_attempts SET root_released_at=1001 "
                         "WHERE attempt_id=%s", (attempt.attempt_id,))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM asset_references WHERE owner=%s",
                            (f"fleet-attempt:{attempt.attempt_id}",)).fetchone()["n"] == 2
        conn.execute("UPDATE fleet_app_policy SET revision=2,changed_at=901")
    with pytest.raises(FleetError, match="attempt_policy_changed"):
        _create(registry)


def test_revoked_queued_release_is_idempotent_and_drops_exact_roots(registry) -> None:
    _seed(registry)
    attempt = _create(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET revoked_at=1000 WHERE attempt_id=%s",
                     (attempt.attempt_id,))
    service = AttemptService(registry.db, registry.clock)
    service.release_queued(attempt.attempt_id)
    with registry.db.transaction() as conn:
        first = conn.execute("SELECT root_released_at FROM fleet_app_attempts "
                             "WHERE attempt_id=%s", (attempt.attempt_id,)).fetchone()
        assert first["root_released_at"] is not None
        assert conn.execute("SELECT count(*) AS n FROM asset_references WHERE owner=%s",
                            (f"fleet-attempt:{attempt.attempt_id}",)).fetchone()["n"] == 0
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                         "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                         "VALUES('player-payload',%s,%s,%s,%s,123,%s,123,1001)",
                         (TARGET_SHA, f"fleet-attempt:{attempt.attempt_id}",
                          "https://example.invalid/target-old.tar.gz", TARGET_SHA,
                          TARGET_SHA))
    service.release_queued(attempt.attempt_id)
    with registry.db.transaction() as conn:
        second = conn.execute("SELECT root_released_at FROM fleet_app_attempts "
                              "WHERE attempt_id=%s", (attempt.attempt_id,)).fetchone()
        assert first == second
    with pytest.raises(FleetError, match="attempt_not_active"):
        _create(registry)


def test_retirement_revokes_and_releases_queued_attempt_atomically(registry) -> None:
    _seed(registry)
    attempt = _create(registry)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,"
                     "device_id) VALUES('player-1',%s,%s,900,900,%s)",
                     ("0" * 64, "1" * 64, DEVICE_ID))
    registry.retire("player-1")
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT revoked_at,root_released_at FROM fleet_app_attempts "
                           "WHERE attempt_id=%s", (attempt.attempt_id,)).fetchone()
        assert row["revoked_at"] is not None and row["root_released_at"] is not None
        assert conn.execute("SELECT count(*) AS n FROM asset_references WHERE owner=%s",
                            (f"fleet-attempt:{attempt.attempt_id}",)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts "
                            "WHERE device_id=%s", (DEVICE_ID,)).fetchone()["n"] == 1
    registry.retire("player-1")
    service = AttemptService(registry.db, registry.clock)
    service.release_queued(attempt.attempt_id)
