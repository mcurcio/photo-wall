"""PostgreSQL gates for immutable, exact-byte queued app attempts."""

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation

import central.fleet.attempt_bytes as attempt_bytes_module
import central.fleet.attempts as attempts_module
import central.fleet.principal as principal_module
from central.assets.handlers import FetchPlayerPayloadHandler
from central.assets.layout import CacheLayout
from central.assets.production import AssetProduction
from central.assets.reader import Opened
from central.assets.store import CacheStore
from central.content_catalog.catalog import device_id_for_serial
from central.fleet.attempt_bytes import AttemptByteAccess
from central.fleet.attempts import AttemptService
from central.fleet.bytes import OfferByteReader
from central.fleet.locks import FLEET_ASSET_LOCK
from central.fleet.models import FleetError
from central.fleet.principal import PrincipalError, VerifiedOsPrincipal
from central.fleet.service import FleetService
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.job_types import FetchPlayerPayload

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
          fallback_qualified: bool = True, target_sha: str = TARGET_SHA,
          target_size: int = 123, fallback_sha: str = FALLBACK_SHA,
          fallback_size: int = 80) -> None:
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,base_tarball_sha256,base_abi,"
                     "base_abi_squashfs_sha256,base_abi_source_manifest) "
                     "VALUES(%s,1,0,0,FALSE,900,900,%s,%s,%s,'manifest.v2.json')",
                     (BASE_TAG, BASE_TARBALL_SHA, BASE_ABI, BASE_SHA))
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "discovered_at,updated_at,mirror_state,payload_url,payload_sha256,"
                     "payload_size,payload_format,payload_base_abi,payload_source_manifest) "
                     "VALUES(%s,1,0,1,FALSE,900,900,'mirrored',%s,%s,%s,"
                     "'pw-player-data-v1',%s,'manifest.v2.json')",
                     (TARGET_TAG, "https://example.invalid/target-old.tar.gz",
                      target_sha, target_size, BASE_ABI))
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
                     "VALUES(TRUE,1,%s,%s,%s,'pw-player-data-v1',900)",
                     (TARGET_TAG, target_sha, target_size))
        if fallback_qualified:
            conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                         "sha256,size,base_abi,trust_mode,evidence_ref,accepted_at) "
                         "VALUES(%s,'app',%s,%s,%s,%s,'t1','qualified-output-proof',900)",
                         (DEVICE_ID, fallback_sha, fallback_sha, fallback_size, BASE_ABI))
            conn.execute("INSERT INTO assets(kind,identity,created_at) "
                         "VALUES('player-payload',%s,900)", (fallback_sha,))
            conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                         "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                         "VALUES('player-payload',%s,%s,%s,%s,%s,%s,%s,900)",
                         (fallback_sha, f"fleet-fallback:{DEVICE_ID}",
                          "https://example.invalid/fallback.tar.gz", fallback_sha,
                          fallback_size, fallback_sha, fallback_size))


def _create(registry, *, target_sha: str = TARGET_SHA, fallback_sha: str = FALLBACK_SHA):
    return AttemptService(registry.db, registry.clock).create_queued(
        _principal(), desired_revision=1, expected_target_sha256=target_sha,
        fallback_sha256=fallback_sha)


def _after_fleet_lock_wait(registry, monkeypatch, action, *, elapsed: float,
                           reverse_utc: bool = False):
    """Make a real PostgreSQL advisory lock hold the service past its first sample."""
    waiting = Event()
    lock = principal_module.lock_fleet_assets_in

    def signalled_lock(conn):
        waiting.set()
        lock(conn)

    monkeypatch.setattr(principal_module, "lock_fleet_assets_in", signalled_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (FLEET_ASSET_LOCK,))
            future = pool.submit(action)
            assert waiting.wait(timeout=3)
            assert not future.done()
            registry.clock.advance(elapsed)
            if reverse_utc:
                registry.clock.step_utc(-elapsed)
        return future.result(timeout=5)


def _after_attempt_lock_wait(registry, monkeypatch, module, attempt_id, action, *,
                             elapsed: float, reverse_utc: bool = False):
    """Hold the attempt row after the OS session was admitted under its locks."""
    waiting = Event()
    guard = module.require_current_principal_in

    def signalled_guard(conn, principal, *, clock):
        admission = guard(conn, principal, clock=clock)
        waiting.set()
        return admission

    monkeypatch.setattr(module, "require_current_principal_in", signalled_guard)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT attempt_id FROM fleet_app_attempts "
                            "WHERE attempt_id=%s FOR UPDATE", (attempt_id,))
            future = pool.submit(action)
            assert waiting.wait(timeout=3)
            assert not future.done()
            registry.clock.advance(elapsed)
            if reverse_utc:
                registry.clock.step_utc(-elapsed)
        return future.result(timeout=5)


@pytest.mark.parametrize("reverse_utc", [False, True])
def test_queued_attempt_rejects_session_expired_during_fleet_lock_wait(
    registry, monkeypatch, reverse_utc,
) -> None:
    _seed(registry)
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        _after_fleet_lock_wait(registry, monkeypatch, lambda: _create(registry),
                               elapsed=101, reverse_utc=reverse_utc)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM asset_references "
                            "WHERE owner LIKE 'fleet-attempt:%'").fetchone()["n"] == 0


def test_queued_attempt_timestamps_use_post_lock_admission_time(registry, monkeypatch) -> None:
    _seed(registry)
    attempt = _after_fleet_lock_wait(registry, monkeypatch, lambda: _create(registry),
                                     elapsed=5)
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT created_at,updated_at FROM fleet_app_attempts "
                           "WHERE attempt_id=%s", (attempt.attempt_id,)).fetchone()
    assert row == {"created_at": 1005, "updated_at": 1005}


@pytest.mark.parametrize("reverse_utc", [False, True])
def test_queued_retry_rejects_expiry_during_attempt_row_lock_wait(
    registry, monkeypatch, reverse_utc,
) -> None:
    _seed(registry)
    attempt = _create(registry)
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        _after_attempt_lock_wait(
            registry, monkeypatch, attempts_module, attempt.attempt_id,
            lambda: _create(registry), elapsed=101, reverse_utc=reverse_utc,
        )


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
    with pytest.raises(CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute(
                "INSERT INTO fleet_app_attempts SELECT (jsonb_populate_record("
                "NULL::fleet_app_attempts,to_jsonb(a) || jsonb_build_object("
                "'attempt_id',%s::text,'desired_revision',3,'fallback_sha256',"
                "a.target_sha256))).* FROM fleet_app_attempts AS a WHERE a.attempt_id=%s",
                (UUID(int=907), attempt.attempt_id),
            )


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


class _FileReader:
    def __init__(self, paths, *, after_read=None):
        self.paths = paths
        self.after_read = after_read
        self.fds = []

    async def read(self, candidates):
        job = candidates.jobs[0]
        path = self.paths[job.sha256]
        fd = os.open(path, os.O_RDONLY)
        self.fds.append(fd)
        if self.after_read is not None:
            self.after_read()
        return Opened(job, fd, os.fstat(fd).st_size, job.sha256)


def _seed_file_attempt(registry, tmp_path):
    target_bytes, fallback_bytes = b"attempt target bytes", b"accepted fallback bytes"
    target_sha = hashlib.sha256(target_bytes).hexdigest()
    fallback_sha = hashlib.sha256(fallback_bytes).hexdigest()
    target_path, fallback_path = tmp_path / "target", tmp_path / "fallback"
    target_path.write_bytes(target_bytes)
    fallback_path.write_bytes(fallback_bytes)
    _seed(registry, target_sha=target_sha, target_size=len(target_bytes),
          fallback_sha=fallback_sha, fallback_size=len(fallback_bytes))
    attempt = _create(registry, target_sha=target_sha, fallback_sha=fallback_sha)
    reader = _FileReader({target_sha: target_path, fallback_sha: fallback_path})
    return attempt, reader, target_sha, fallback_sha


def test_attempt_byte_access_preflights_same_pod_and_survives_catalog_recut(registry,
                                                                             tmp_path,
                                                                             monkeypatch) -> None:
    attempt, reader, target_sha, fallback_sha = _seed_file_attempt(registry, tmp_path)
    access = AttemptByteAccess(registry.db, registry.clock, OfferByteReader(reader))
    assert asyncio.run(access.preflight(_principal(), attempt.attempt_id)) == attempt
    assert len(reader.fds) == 2
    for fd in reader.fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    releases = PgReleaseRecords()
    # Matching release/accepted and attempt claims must collapse to one ABI.
    with PgTransactions(registry.db).begin() as tx:
        assert releases.payload_abi_for(tx, target_sha, now=1000) == BASE_ABI
        assert releases.payload_abi_for(tx, fallback_sha, now=1000) == BASE_ABI
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET payload_url=%s,payload_sha256=%s "
                     "WHERE tag=%s", ("https://example.invalid/recut.tar.gz",
                                      "0" * 64, TARGET_TAG))
        conn.execute("UPDATE fleet_boot_offers SET expires_at=999 WHERE offer_id=%s",
                     (OFFER_ID,))
        conn.execute("DELETE FROM asset_references WHERE owner=%s",
                     (f"fleet-fallback:{DEVICE_ID}",))
    with PgTransactions(registry.db).begin() as tx:
        assert releases.payload_abi_for(tx, target_sha, now=1000) == BASE_ABI
        assert releases.payload_abi_for(tx, fallback_sha, now=1000) == BASE_ABI

    class FrozenOrigin:
        async def download(self, locator, destination, *, max_bytes):
            expected = {
                "https://example.invalid/target-old.tar.gz":
                    reader.paths[target_sha].read_bytes(),
                "https://example.invalid/fallback.tar.gz":
                    reader.paths[fallback_sha].read_bytes(),
            }
            assert locator.url in expected
            assert max_bytes == len(expected[locator.url])
            destination.write_bytes(expected[locator.url])

    async def expected_abi(sha256):
        with PgTransactions(registry.db).begin() as tx:
            return releases.payload_abi_for(tx, sha256, now=1000)

    monkeypatch.setattr("central.assets.handlers.verify_archive",
                        lambda _path: {"base_abi": BASE_ABI})
    store = CacheStore(CacheLayout(tmp_path / "rehydrated-cache"))
    handler = FetchPlayerPayloadHandler(
        production=AssetProduction(store=store, records=PgAssetRecords(registry.clock),
                                   transactions=PgTransactions(registry.db)),
        origin=FrozenOrigin(), expected_abi=expected_abi)
    for sha in (target_sha, fallback_sha):
        assert asyncio.run(handler.handle(FetchPlayerPayload(sha256=sha))).sha256 == sha
    opened = asyncio.run(access.open_role(_principal(), attempt.attempt_id, "target"))
    try:
        assert os.read(opened.fd, 100) == b"attempt target bytes"
    finally:
        os.close(opened.fd)


def test_attempt_byte_access_rejects_wrong_session_and_bad_bytes(registry, tmp_path) -> None:
    attempt, reader, target_sha, _ = _seed_file_attempt(registry, tmp_path)
    access = AttemptByteAccess(registry.db, registry.clock, OfferByteReader(reader))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        access.resolve(replace(_principal(), kernel_boot_id=UUID(int=999)),
                       attempt.attempt_id, "target")
    with pytest.raises(FleetError, match="attempt_artifact_request_invalid"):
        access.resolve(_principal(), attempt.attempt_id, "unknown")
    reader.paths[target_sha].write_bytes(b"wrong bytes")
    with pytest.raises(FleetError, match="offer_artifact_mismatch"):
        asyncio.run(access.preflight(_principal(), attempt.attempt_id))
    for fd in reader.fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("reverse_utc", [False, True])
def test_attempt_bytes_cannot_open_after_session_expires_during_fleet_lock_wait(
    registry, tmp_path, monkeypatch, reverse_utc,
) -> None:
    attempt, reader, _, _ = _seed_file_attempt(registry, tmp_path)
    access = AttemptByteAccess(registry.db, registry.clock, OfferByteReader(reader))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        _after_fleet_lock_wait(
            registry, monkeypatch,
            lambda: asyncio.run(access.open_role(_principal(), attempt.attempt_id, "target")),
            elapsed=101, reverse_utc=reverse_utc,
        )
    assert reader.fds == []


@pytest.mark.parametrize("reverse_utc", [False, True])
def test_attempt_bytes_cannot_open_after_expiry_during_attempt_row_lock_wait(
    registry, tmp_path, monkeypatch, reverse_utc,
) -> None:
    attempt, reader, _, _ = _seed_file_attempt(registry, tmp_path)
    access = AttemptByteAccess(registry.db, registry.clock, OfferByteReader(reader))
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        _after_attempt_lock_wait(
            registry, monkeypatch, attempt_bytes_module, attempt.attempt_id,
            lambda: asyncio.run(access.open_role(_principal(), attempt.attempt_id, "target")),
            elapsed=101, reverse_utc=reverse_utc,
        )
    assert reader.fds == []


def test_attempt_open_rechecks_after_release_and_closes_fd(registry, tmp_path) -> None:
    attempt, reader, _, _ = _seed_file_attempt(registry, tmp_path)

    def revoke_and_release():
        reader.after_read = None
        with registry.db.transaction() as conn:
            conn.execute("UPDATE fleet_app_attempts SET revoked_at=1000 WHERE attempt_id=%s",
                         (attempt.attempt_id,))
        AttemptService(registry.db, registry.clock).release_queued(attempt.attempt_id)

    reader.after_read = revoke_and_release
    access = AttemptByteAccess(registry.db, registry.clock, OfferByteReader(reader))
    with pytest.raises(FleetError, match="attempt_artifact_unavailable"):
        asyncio.run(access.open_role(_principal(), attempt.attempt_id, "target"))
    for fd in reader.fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_committed_revoked_attempt_bytes_stay_available_to_current_session(registry,
                                                                            tmp_path) -> None:
    attempt, reader, _, _ = _seed_file_attempt(registry, tmp_path)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET phase='stop_committed',"
                     "command_id=%s,drain_id=%s,revoked_at=1000 WHERE attempt_id=%s",
                     (UUID(int=905), UUID(int=906), attempt.attempt_id))
    access = AttemptByteAccess(registry.db, registry.clock, OfferByteReader(reader))
    assert access.resolve(_principal(), attempt.attempt_id, "fallback").sha256 == \
        attempt.fallback_sha256
    registry.clock.advance(101)
    with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
        access.resolve(_principal(), attempt.attempt_id, "fallback")
