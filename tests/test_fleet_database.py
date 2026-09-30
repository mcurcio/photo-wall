"""Real PostgreSQL gates for migration 036 and immutable T0 transactions.

The shared registry fixture supplies a private migrated schema and skips without
PHOTO_WALL_TEST_DATABASE_URL; this test must run against local Compose and CI PostgreSQL.
"""

import asyncio
import hashlib
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central.assets.handlers import FetchPlayerPayloadHandler
from central.assets.layout import CacheLayout
from central.assets.production import AssetProduction
from central.assets.reader import Opened
from central.assets.store import CacheStore
from central.fleet.bytes import OfferByteReader
from central.fleet.fallback import (
    AcceptedFallbackService,
    retire_device_fallback_references,
)
from central.fleet.models import (
    Artifact,
    BaselineWrite,
    CheckIn,
    FleetError,
    OfferRequest,
    PolicyWrite,
)
from central.fleet.routes import mount_fleet_routes
from central.fleet.service import OFFER_TTL_SECONDS, FleetService
from central.infra.asset_records import PgAssetRecords
from central.infra.catalog_records import PgReleaseRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.kernel.job_types import FetchPlayerPayload
from central.netboot_base import record_base_health
from contracts.models import BaseHealth

APP_SHA = "a" * 64
LEGACY_SHA = "f" * 64
BASE_ABI = "sha256:" + "c" * 64
TARBALL_SHA = "b" * 64
BASE_BYTES = b"measured database route base"
BASE_SHA = hashlib.sha256(BASE_BYTES).hexdigest()
SERIAL = "abcdef1234567890"
TAG = "v1.2.3"


def _seed_release(registry) -> None:
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,asset_sha256,"
            "asset_size,mirror_state,discovered_at,updated_at,base_tarball_sha256,"
            "base_tarball_url,base_tarball_size,base_abi,base_abi_squashfs_sha256,"
            "base_abi_source_manifest,"
            "payload_url,payload_sha256,payload_size,payload_format,payload_base_abi,"
            "payload_source_manifest) "
            "VALUES(%s,1,2,3,FALSE,%s,123,'mirrored',1,1,%s,%s,100,%s,%s,%s,"
            "%s,%s,123,%s,%s,%s)",
            (TAG, LEGACY_SHA, TARBALL_SHA, "https://example.invalid/base.tar.gz",
             BASE_ABI, BASE_SHA, "manifest.v2.json",
             "https://example.invalid/payload.tar.gz",
             APP_SHA, "pw-player-data-v1", BASE_ABI, "manifest.v2.json"),
        )
        conn.execute(
            "INSERT INTO base_cache(tag,squashfs_sha256,size,state,updated_at) "
            "VALUES(%s,%s,%s,'cached',1)", (TAG, BASE_SHA, len(BASE_BYTES)),
        )


def _request(boot: int, nonce: str) -> OfferRequest:
    return OfferRequest(schema=1, kind="pi", serial=SERIAL,
                        kernel_boot_id=UUID(int=boot), boot_nonce=nonce * 32)


def _accept_app(registry, device_id: str, digest: str, size: int,
                evidence_ref: str, *, trust_mode: str = "t1") -> None:
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                     "sha256,size,base_abi,trust_mode,evidence_ref,accepted_at) "
                     "VALUES(%s,'app',%s,%s,%s,%s,%s,%s,%s)",
                     (device_id, digest, digest, size, BASE_ABI, trust_mode,
                      evidence_ref, registry.clock.utc()))


def _two_accepted_payloads(registry):
    """Two frozen boot offers on one device, with a mutable tag recut between them."""
    _seed_release(registry)
    fleet = FleetService(registry.db, registry.clock)
    fleet.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    first_policy = fleet.set_app_policy(PolicyWrite(
        expected_revision=0, target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    fleet.create_offer(_request(54, "d"))
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
    _accept_app(registry, device_id, APP_SHA, 123, "qualified:54")
    empty = fleet.set_app_policy(PolicyWrite(expected_revision=first_policy["revision"],
                                             target=None))
    second_sha = "d" * 64
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET payload_url=%s,payload_sha256=%s,"
                     "payload_size=124 WHERE tag=%s",
                     ("https://example.invalid/new-payload.tar.gz", second_sha, TAG))
    second_policy = fleet.set_app_policy(PolicyWrite(
        expected_revision=empty["revision"],
        target=Artifact(tag=TAG, sha256=second_sha, size=124)))
    second_offer = fleet.create_offer(_request(55, "e"))
    _accept_app(registry, device_id, second_sha, 124, "qualified:55")
    return fleet, AcceptedFallbackService(registry.db, registry.clock), \
        device_id, second_sha, second_offer, second_policy


def _copy_attempt_root(conn, attempt_id, device_id: str, digest: str) -> None:
    conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                 "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                 "SELECT kind,identity,%s,locator_url,locator_sha256,locator_size,"
                 "expected_sha256,expected_size,added_at FROM asset_references "
                 "WHERE kind='player-payload' AND identity=%s AND owner=%s",
                 (f"fleet-attempt:{attempt_id}", digest,
                  f"fleet-fallback:{device_id}"))


def test_offer_freezes_exact_pair_provenance_and_expiry(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    baseline = service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    policy = service.set_app_policy(PolicyWrite(
        expected_revision=0, target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    first = service.create_offer(_request(1, "a"))
    assert first["base"] == {"tag": TAG, "sha256": BASE_SHA, "size": len(BASE_BYTES)}
    assert first["schema"] == 2
    assert first["initial_app"] == {"tag": TAG, "sha256": APP_SHA, "size": 123,
                                    "format": "pw-player-data-v1", "base_abi": BASE_ABI}
    assert first["base_policy_revision"] == baseline["revision"]
    assert first["app_policy_revision"] == policy["revision"]
    assert first["app_policy_source"] == "explicit"
    assert first["compatibility_basis"] == "abi_match"
    service.set_app_policy(PolicyWrite(expected_revision=policy["revision"], target=None))
    assert service.create_offer(_request(1, "a")) == first
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        quota = conn.execute("SELECT used FROM fleet_t0_daily_quotas "
                             "WHERE scope=%s AND kind='offer'",
                             (device_id,)).fetchone()
        assert quota["used"] == 2  # duplicate still pays for its byte preflight
        roots = conn.execute("SELECT kind,content_key,sha256,size "
                             "FROM fleet_offer_artifact_roots "
                             "WHERE offer_id=%s ORDER BY kind", (first["offer_id"],)).fetchall()
    assert roots == [{"kind": "app", "content_key": APP_SHA,
                      "sha256": APP_SHA, "size": 123},
                     {"kind": "base", "content_key": TARBALL_SHA,
                      "sha256": BASE_SHA, "size": len(BASE_BYTES)}]
    evicted = []
    assert not service.evict_if_unretained(
        kind="base", content_key=TARBALL_SHA, evict=lambda: evicted.append("base"))
    assert evicted == []
    registry.clock.advance(OFFER_TTL_SECONDS)
    with pytest.raises(FleetError, match="boot_offer_expired"):
        service.create_offer(_request(1, "a"))
    with pytest.raises(FleetError, match="boot_offer_expired"):
        service.offer_asset(UUID(first["offer_id"]), "base")
    assert service.evict_if_unretained(
        kind="base", content_key=TARBALL_SHA, evict=lambda: evicted.append("base"))
    assert evicted == ["base"]


def test_check_in_sequence_and_delayed_boot_remain_observational(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    older = service.create_offer(_request(1, "a"))
    newer = service.create_offer(_request(2, "b"))

    def report(boot: int, offer_id: str, sequence: int) -> CheckIn:
        return CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=boot),
                       boot_nonce=("a" if boot == 1 else "b") * 32,
                       offer_id=UUID(offer_id), base_digest=BASE_SHA,
                       agent_incarnation="agent-1", observation_sequence=sequence,
                       phase="base_ready")

    assert service.record_check_in(report(2, newer["offer_id"], 2)) == {"accepted": True}
    assert service.record_check_in(report(1, older["offer_id"], 1)) == {"accepted": True}
    assert service.record_check_in(report(2, newer["offer_id"], 1)) == {
        "accepted": False, "reason": "stale_or_duplicate", "next_sequence": 3}
    device = next(d for d in service.status()["devices"] if d["serial"] == SERIAL)
    assert device["base"]["state"] == "ambiguous_boot_claims"
    assert device["base"]["current_physical_boot"] == "unknown"
    assert device["offered"]["boot_id"] == str(UUID(int=2))


def test_duplicate_check_ins_consume_quota_before_idempotent_receipt(registry) -> None:
    service = FleetService(registry.db, registry.clock)
    report = CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=91),
                     agent_incarnation="agent-1", observation_sequence=1,
                     phase="base_ready")
    assert service.record_check_in(report) == {"accepted": True}
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        conn.execute("UPDATE fleet_t0_daily_quotas SET used=9999 "
                     "WHERE scope=%s AND kind='observation'", (device_id,))
    assert service.record_check_in(report) == {
        "accepted": False, "reason": "stale_or_duplicate", "next_sequence": 2}
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT used FROM fleet_t0_daily_quotas "
                            "WHERE scope=%s AND kind='observation'", (device_id,)
                            ).fetchone()["used"] == 10000
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_observations "
                            "WHERE device_id=%s", (device_id,)).fetchone()["n"] == 1
    with pytest.raises(FleetError, match="t0_rate_limited"):
        service.record_check_in(report)


def test_mismatched_offer_check_in_pays_quota_before_lookup(registry) -> None:
    service = FleetService(registry.db, registry.clock)
    report = CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=92),
                     offer_id=UUID(int=999), agent_incarnation="agent-1",
                     observation_sequence=1, phase="base_ready")
    with pytest.raises(FleetError, match="boot_offer_mismatch"):
        service.record_check_in(report)
    with registry.db.transaction() as conn:
        charged = conn.execute("SELECT scope,used FROM fleet_t0_daily_quotas "
                               "WHERE kind='observation' ORDER BY scope").fetchall()
        assert len(charged) == 2 and all(row["used"] == 1 for row in charged)
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_observations").fetchone()[
            "n"] == 0


def test_new_offer_never_selects_payload_with_incompatible_base_abi(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    policy = service.set_app_policy(PolicyWrite(
        expected_revision=0, target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,"
                     "mirror_state,discovered_at,updated_at,base_tarball_sha256,"
                     "base_tarball_url,base_tarball_size,base_abi,"
                     "base_abi_squashfs_sha256,base_abi_source_manifest,"
                     "payload_url,payload_sha256,payload_size,payload_format,"
                     "payload_base_abi,payload_source_manifest) "
                     "VALUES('v1.2.4',1,2,4,FALSE,'mirrored',1,1,%s,%s,100,%s,%s,%s,"
                     "%s,%s,123,%s,%s,%s)",
                     ("d" * 64, "https://example.invalid/other-base.tar.gz",
                      "sha256:" + "d" * 64, "d" * 64, "manifest.v2.json",
                      "https://example.invalid/other.tar.gz", "e" * 64,
                      "pw-player-data-v1", "sha256:" + "d" * 64,
                      "manifest.v2.json"))
    service.set_app_policy(PolicyWrite(expected_revision=policy["revision"],
                                      target=Artifact(tag="v1.2.4", sha256="e" * 64,
                                                      size=123)))
    offer = service.create_offer(_request(3, "c"))
    assert offer["schema"] == 2
    assert offer["initial_app"] is None
    assert offer["initial_app_status"] == "compatibility_unverified"
    assert offer["compatibility_basis"] == "none"


def test_historical_schema_one_offer_remains_readable(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    service.set_app_policy(PolicyWrite(expected_revision=0,
                                      target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    created = service.create_offer(_request(4, "d"))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_boot_offers SET offer_schema=1,app_format=NULL,"
                     "app_base_abi=NULL WHERE offer_id=%s", (created["offer_id"],))
    historical = service.create_offer(_request(4, "d"))
    assert historical["schema"] == 1
    assert historical["initial_app"] == {"tag": TAG, "sha256": APP_SHA, "size": 123}
    assert service.offer_asset(UUID(created["offer_id"]), "app").format is None


def test_incapable_base_refuses_schema_two_even_with_no_app(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET base_abi=NULL,"
                     "base_abi_squashfs_sha256=NULL,base_abi_source_manifest=NULL "
                     "WHERE tag=%s", (TAG,))
    with pytest.raises(FleetError, match="base_incapable"):
        service.create_offer(_request(6, "f"))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_boot_offers").fetchone()[
            "n"] == 0


def test_offer_owned_references_survive_release_recut_until_expiry(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    service.set_app_policy(PolicyWrite(expected_revision=0,
                                      target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    offer = service.create_offer(_request(7, "7"))
    owner = f"fleet-offer:{offer['offer_id']}"
    with registry.db.transaction() as conn:
        # Catalog sync retires a recut release's tag-owned refs, not the offer.
        conn.execute("DELETE FROM asset_references WHERE owner=%s", (TAG,))
        conn.execute("UPDATE app_releases SET payload_url=%s,payload_sha256=%s "
                     "WHERE tag=%s", ("https://example.invalid/recut.tar.gz", "8" * 64, TAG))
    records = PgAssetRecords(registry.clock)
    transactions = PgTransactions(registry.db)
    with transactions.begin() as tx:
        app = records.get(tx, AssetKey(AssetKind.PLAYER_PAYLOAD, APP_SHA))
        base = records.get(tx, AssetKey(AssetKind.OS_IMAGE, TARBALL_SHA))
    assert app is not None and app.references[0].owner == owner
    assert base is not None and base.references[0].owner == owner
    assert service.offer_asset(UUID(offer["offer_id"]), "app").sha256 == APP_SHA
    registry.clock.advance(OFFER_TTL_SECONDS)
    # The next transaction retires expired refs without changing the old offer.
    with pytest.raises(FleetError, match="boot_offer_expired"):
        service.create_offer(_request(7, "7"))
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM asset_references "
                            "WHERE owner=%s", (owner,)).fetchone()["n"] == 0


def test_accepted_fallback_retains_exact_locator_after_offer_expiry(registry) -> None:
    _seed_release(registry)
    fleet = FleetService(registry.db, registry.clock)
    fleet.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    fleet.set_app_policy(PolicyWrite(expected_revision=0,
                                     target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    fleet.create_offer(_request(51, "a"))
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
    retention = AcceptedFallbackService(registry.db, registry.clock)
    with pytest.raises(FleetError, match="fallback_not_qualified"):
        retention.reserve_app(device_id=device_id, sha256=APP_SHA, base_abi=BASE_ABI)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                     "sha256,size,base_abi,trust_mode,evidence_ref,accepted_at) "
                     "VALUES(%s,'app',%s,%s,123,%s,'t1','qualified:51',%s)",
                     (device_id, APP_SHA, APP_SHA, BASE_ABI, registry.clock.utc()))
    with pytest.raises(FleetError, match="fallback_not_qualified"):
        retention.reserve_app(device_id=device_id, sha256=APP_SHA,
                              base_abi="sha256:" + "e" * 64)
    fallback = retention.reserve_app(device_id=device_id, sha256=APP_SHA,
                                     base_abi=BASE_ABI)
    assert fallback.sha256 == APP_SHA
    assert fallback.trust_mode == "t1"
    assert fallback.evidence_ref == "qualified:51"
    assert fallback.asset.format == "pw-player-data-v1"
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET payload_url=%s,payload_sha256=%s "
                     "WHERE tag=%s", ("https://example.invalid/recut.tar.gz", "8" * 64, TAG))
    registry.clock.advance(OFFER_TTL_SECONDS)
    with pytest.raises(FleetError, match="boot_offer_expired"):
        fleet.create_offer(_request(51, "a"))
    with registry.db.transaction() as conn:
        ref = conn.execute("SELECT locator_url,locator_sha256 FROM asset_references "
                           "WHERE kind='player-payload' AND identity=%s AND owner=%s",
                           (APP_SHA, f"fleet-fallback:{device_id}")).fetchone()
    assert ref == {"locator_url": "https://example.invalid/payload.tar.gz",
                   "locator_sha256": APP_SHA}
    assert retention.reserve_app(device_id=device_id, sha256=APP_SHA,
                                 base_abi=BASE_ABI) == fallback
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA in desired.player_payloads
    assert not fleet.evict_if_unretained(kind="app", content_key=APP_SHA,
                                        evict=lambda: pytest.fail("retained fallback evicted"))


def test_retired_device_keeps_fallback_history_but_releases_bytes(registry) -> None:
    _seed_release(registry)
    fleet = FleetService(registry.db, registry.clock)
    fleet.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    policy = fleet.set_app_policy(PolicyWrite(
        expected_revision=0, target=Artifact(tag=TAG, sha256=APP_SHA, size=123)))
    fleet.create_offer(_request(52, "b"))
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                     "sha256,size,base_abi,trust_mode,evidence_ref,accepted_at) "
                     "VALUES(%s,'app',%s,%s,123,%s,'t2','qualified:52',%s)",
                     (device_id, APP_SHA, APP_SHA, BASE_ABI, registry.clock.utc()))
    retention = AcceptedFallbackService(registry.db, registry.clock)
    retention.reserve_app(device_id=device_id, sha256=APP_SHA, base_abi=BASE_ABI)
    with registry.db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (734118328,))
        conn.execute("UPDATE devices SET retired_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), device_id))
        retire_device_fallback_references(conn, device_id)
        assert conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts "
                            "WHERE device_id=%s", (device_id,)).fetchone()["n"] == 1
    with pytest.raises(FleetError, match="fallback_device_unavailable"):
        retention.reserve_app(device_id=device_id, sha256=APP_SHA, base_abi=BASE_ABI)
    fleet.set_app_policy(PolicyWrite(expected_revision=policy["revision"], target=None))
    registry.clock.advance(OFFER_TTL_SECONDS)
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA not in desired.player_payloads
    evicted = []
    assert fleet.evict_if_unretained(kind="app", content_key=APP_SHA,
                                    evict=lambda: evicted.append(APP_SHA))
    assert evicted == [APP_SHA]
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM asset_references WHERE owner=%s",
                            (f"fleet-fallback:{device_id}",)).fetchone()["n"] == 0


def test_retired_device_override_is_history_not_a_prefetch_root(registry) -> None:
    _seed_release(registry)
    fleet = FleetService(registry.db, registry.clock)
    fleet.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    fleet.create_offer(_request(56, "f"))
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
    fleet.set_override(device_id, expected_revision=0,
                       target=Artifact(tag=TAG, sha256=APP_SHA, size=123))
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA in desired.player_payloads
    with registry.db.transaction() as conn:
        conn.execute("UPDATE devices SET retired_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), device_id))
        conn.execute("UPDATE fleet_device_lifecycle SET generation=generation+1,"
                     "revoked_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), device_id))
        assert conn.execute("SELECT count(*) AS n FROM fleet_device_app_overrides "
                            "WHERE device_id=%s", (device_id,)).fetchone()["n"] == 1
    registry.clock.advance(OFFER_TTL_SECONDS)
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA not in desired.player_payloads


def test_accepted_fallback_preflight_hashes_local_bytes(registry, tmp_path,
                                                        monkeypatch) -> None:
    _seed_release(registry)
    blob = b"verified fallback bytes"
    digest = hashlib.sha256(blob).hexdigest()
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET payload_sha256=%s,payload_size=%s "
                     "WHERE tag=%s", (digest, len(blob), TAG))
    fleet = FleetService(registry.db, registry.clock)
    fleet.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    fleet.set_app_policy(PolicyWrite(expected_revision=0,
                                     target=Artifact(tag=TAG, sha256=digest,
                                                     size=len(blob))))
    fleet.create_offer(_request(53, "c"))
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                     "sha256,size,base_abi,trust_mode,evidence_ref,accepted_at) "
                     "VALUES(%s,'app',%s,%s,%s,%s,'t1','qualified:53',%s)",
                     (device_id, digest, digest, len(blob), BASE_ABI, registry.clock.utc()))
    path = tmp_path / "fallback.tar.gz"
    path.write_bytes(blob)

    class LocalReader:
        async def read(self, candidates):
            fd = os.open(path, os.O_RDONLY)
            return Opened(candidates.jobs[0], fd, len(blob), "f" * 64)

    retention = AcceptedFallbackService(registry.db, registry.clock)
    bytes_reader = OfferByteReader(LocalReader())
    assert asyncio.run(retention.preflight_app(
        device_id=device_id, sha256=digest, base_abi=BASE_ABI,
        bytes_reader=bytes_reader)).sha256 == digest
    path.write_bytes(b"wrong fallback bytes")
    with pytest.raises(FleetError, match="offer_artifact_mismatch"):
        asyncio.run(retention.preflight_app(device_id=device_id, sha256=digest,
                                           base_abi=BASE_ABI, bytes_reader=bytes_reader))

    # After recut and offer expiry the only exact locator/ABI source is the
    # accepted fallback reservation. The worker can still rehydrate a wiped cache.
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET payload_url=%s,payload_sha256=%s "
                     "WHERE tag=%s", ("https://example.invalid/recut.tar.gz", "8" * 64, TAG))
    registry.clock.advance(OFFER_TTL_SECONDS)
    with pytest.raises(FleetError, match="boot_offer_expired"):
        fleet.create_offer(_request(53, "c"))
    releases = PgReleaseRecords()
    with PgTransactions(registry.db).begin() as tx:
        assert releases.payload_abi_for(tx, digest, now=registry.clock.utc()) == BASE_ABI
        asset = PgAssetRecords(registry.clock).get(
            tx, AssetKey(AssetKind.PLAYER_PAYLOAD, digest))
    assert asset is not None
    assert [ref.owner for ref in asset.references] == [f"fleet-fallback:{device_id}"]

    class FrozenOrigin:
        async def download(self, locator, destination, *, max_bytes):
            assert locator.url == "https://example.invalid/payload.tar.gz"
            assert max_bytes == len(blob)
            destination.write_bytes(blob)

    async def expected_abi(sha256):
        with PgTransactions(registry.db).begin() as tx:
            return releases.payload_abi_for(tx, sha256, now=registry.clock.utc())

    monkeypatch.setattr("central.assets.handlers.verify_archive",
                        lambda _path: {"base_abi": BASE_ABI})
    store = CacheStore(CacheLayout(tmp_path / "rehydrated-cache"))
    handler = FetchPlayerPayloadHandler(
        production=AssetProduction(store=store, records=PgAssetRecords(registry.clock),
                                   transactions=PgTransactions(registry.db)),
        origin=FrozenOrigin(), expected_abi=expected_abi)
    asyncio.run(handler.handle(FetchPlayerPayload(sha256=digest)))
    assert store.layout.path(AssetKey(AssetKind.PLAYER_PAYLOAD, digest)).read_bytes() == blob


def test_accepted_fallback_reservation_is_idempotent_and_rotates_one_root(registry) -> None:
    _, retention, device_id, second_sha, _, _ = _two_accepted_payloads(registry)
    first = retention.reserve_app(device_id=device_id, sha256=APP_SHA, base_abi=BASE_ABI)
    assert retention.reserve_app(device_id=device_id, sha256=APP_SHA,
                                 base_abi=BASE_ABI) == first
    second = retention.reserve_app(device_id=device_id, sha256=second_sha,
                                   base_abi=BASE_ABI)
    assert second.sha256 == second_sha
    with registry.db.transaction() as conn:
        roots = conn.execute("SELECT identity,locator_url FROM asset_references "
                             "WHERE kind='player-payload' AND owner=%s",
                             (f"fleet-fallback:{device_id}",)).fetchall()
        accepted = conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts "
                                "WHERE device_id=%s AND kind='app'", (device_id,)
                                ).fetchone()["n"]
    assert roots == [{"identity": second_sha,
                      "locator_url": "https://example.invalid/new-payload.tar.gz"}]
    assert accepted == 2  # Rotation changes byte retention, never historical acceptance.


def test_accepted_fallback_rotation_refuses_unfinished_attempt(registry) -> None:
    _, retention, device_id, second_sha, offer, policy = _two_accepted_payloads(registry)
    retention.reserve_app(device_id=device_id, sha256=APP_SHA, base_abi=BASE_ABI)
    attempt_id = uuid4()
    with registry.db.transaction() as conn:
        generation = conn.execute("SELECT generation FROM fleet_device_lifecycle "
                                  "WHERE device_id=%s", (device_id,)).fetchone()["generation"]
        conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                     "device_generation,desired_revision,target_sha256,fallback_sha256,"
                     "phase,created_at,updated_at) "
                     "VALUES(%s,%s,%s,%s,%s,%s,%s,'prepared',%s,%s)",
                     (attempt_id, device_id, offer["offer_id"], generation,
                      policy["revision"],
                      second_sha, APP_SHA, registry.clock.utc(), registry.clock.utc()))
    with pytest.raises(FleetError, match="fallback_rotation_blocked_by_attempt"):
        retention.reserve_app(device_id=device_id, sha256=second_sha, base_abi=BASE_ABI)
    with registry.db.transaction() as conn:
        roots = conn.execute("SELECT identity FROM asset_references WHERE owner=%s",
                             (f"fleet-fallback:{device_id}",)).fetchall()
    assert roots == [{"identity": APP_SHA}]
    with registry.db.transaction() as conn:
        _copy_attempt_root(conn, attempt_id, device_id, APP_SHA)
    retention.reserve_app(device_id=device_id, sha256=second_sha, base_abi=BASE_ABI)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT identity FROM asset_references WHERE owner=%s",
                            (f"fleet-fallback:{device_id}",)).fetchall() == [
                                {"identity": second_sha}]
        assert conn.execute("SELECT identity FROM asset_references WHERE owner=%s",
                            (f"fleet-attempt:{attempt_id}",)).fetchall() == [
                                {"identity": APP_SHA}]
    registry.clock.advance(OFFER_TTL_SECONDS)
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA in desired.player_payloads  # The active attempt's own root remains.
    assert not FleetService(registry.db, registry.clock).evict_if_unretained(
        kind="app", content_key=APP_SHA,
        evict=lambda: pytest.fail("active attempt bytes evicted"))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET revoked_at=%s WHERE attempt_id=%s",
                     (registry.clock.utc(), attempt_id))
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA not in desired.player_payloads
    evicted = []
    assert FleetService(registry.db, registry.clock).evict_if_unretained(
        kind="app", content_key=APP_SHA, evict=lambda: evicted.append(APP_SHA))
    assert evicted == [APP_SHA]


def test_issued_attempt_retains_exact_bytes_across_revocation_until_release(registry) -> None:
    fleet, retention, device_id, second_sha, offer, policy = _two_accepted_payloads(registry)
    retention.reserve_app(device_id=device_id, sha256=APP_SHA, base_abi=BASE_ABI)
    attempt_id = uuid4()
    with registry.db.transaction() as conn:
        generation = conn.execute("SELECT generation FROM fleet_device_lifecycle "
                                  "WHERE device_id=%s", (device_id,)).fetchone()["generation"]
        conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,"
                     "device_generation,desired_revision,target_sha256,fallback_sha256,"
                     "phase,command_id,drain_id,created_at,updated_at) "
                     "VALUES(%s,%s,%s,%s,%s,%s,%s,'stop_committed',%s,%s,%s,%s)",
                     (attempt_id, device_id, offer["offer_id"], generation,
                      policy["revision"], second_sha, APP_SHA, uuid4(), uuid4(),
                      registry.clock.utc(), registry.clock.utc()))
        _copy_attempt_root(conn, attempt_id, device_id, APP_SHA)
    with registry.db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (734118328,))
        conn.execute("UPDATE devices SET retired_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), device_id))
        conn.execute("UPDATE fleet_device_lifecycle SET generation=generation+1,"
                     "revoked_at=%s WHERE device_id=%s",
                     (registry.clock.utc(), device_id))
        conn.execute("UPDATE fleet_app_attempts SET revoked_at=%s,phase='expired_unknown' "
                     "WHERE attempt_id=%s", (registry.clock.utc(), attempt_id))
        retire_device_fallback_references(conn, device_id)
    registry.clock.advance(OFFER_TTL_SECONDS)
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA in desired.player_payloads
    assert not fleet.evict_if_unretained(
        kind="app", content_key=APP_SHA,
        evict=lambda: pytest.fail("unresolved issued attempt evicted"))
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_app_attempts SET root_released_at=%s "
                     "WHERE attempt_id=%s", (registry.clock.utc(), attempt_id))
    with PgTransactions(registry.db).begin() as tx:
        desired = PgReleaseRecords().fleet_desired_assets(tx, now=registry.clock.utc())
    assert APP_SHA not in desired.player_payloads
    evicted = []
    assert fleet.evict_if_unretained(kind="app", content_key=APP_SHA,
                                    evict=lambda: evicted.append(APP_SHA))
    assert evicted == [APP_SHA]


def test_repeated_exact_byte_reads_consume_a_separate_quota(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    offer = service.create_offer(_request(8, "8"))
    service.offer_asset(UUID(offer["offer_id"]), "base")
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        assert conn.execute("SELECT used FROM fleet_t0_daily_quotas "
                            "WHERE scope=%s AND kind='asset'", (device_id,)).fetchone()[
            "used"] == 1
        conn.execute("UPDATE fleet_t0_daily_quotas SET used=512 "
                     "WHERE scope=%s AND kind='asset'", (device_id,))
    with pytest.raises(FleetError, match="t0_rate_limited"):
        service.offer_asset(UUID(offer["offer_id"]), "base")


def test_legacy_policy_digest_is_not_reinterpreted_as_payload(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    with registry.db.transaction() as conn:
        # A previous Central binary omits target_format when writing a .deb.
        conn.execute("INSERT INTO fleet_app_policy(singleton,revision,target_tag,"
                     "target_sha256,target_size,changed_at) VALUES(TRUE,2,%s,%s,123,1)",
                     (TAG, LEGACY_SHA))
    offer = service.create_offer(_request(5, "e"))
    assert offer["initial_app"] is None
    assert offer["initial_app_status"] == "unavailable"
    assert service.status()["fleet_policy"]["target"]["format"] == "player-deb"
    with pytest.raises(psycopg.errors.CheckViolation):
        with registry.db.transaction() as conn:
            # An old writer cannot silently change a new payload row's digest
            # while leaving its format at pw-player-data-v1.
            conn.execute("UPDATE fleet_app_policy SET target_format=%s,"
                         "target_sha256=%s WHERE singleton",
                         ("pw-player-data-v1", APP_SHA))
            conn.execute("UPDATE fleet_app_policy SET target_sha256=%s WHERE singleton",
                         (LEGACY_SHA,))


def test_mounted_offer_route_uses_migrated_selection_and_exact_open(registry, tmp_path) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    path = tmp_path / "base.squashfs"
    path.write_bytes(BASE_BYTES)
    jobs = []

    class Reader:
        async def read(self, candidates):
            jobs.append(candidates.jobs[0])
            return Opened(candidates.jobs[0], os.open(path, os.O_RDONLY), len(BASE_BYTES),
                          "0" * 64)

    app = FastAPI()
    mount_fleet_routes(app, db=registry.db, clock=registry.clock, admin=lambda: None,
                       content=SimpleNamespace(reader=Reader()))
    client = TestClient(app)
    body = _request(1, "a").model_dump(by_alias=True, mode="json")
    first = client.post("/v1/netboot/offers", json=body)
    assert first.status_code == 200, (first.json(), jobs)
    assert first.json()["initial_app"] is None
    assert client.post("/v1/netboot/offers", json=body).json() == first.json()
    got = client.get(f"/v1/netboot/offers/{first.json()['offer_id']}/base")
    assert got.status_code == 200
    assert got.content == BASE_BYTES
    with registry.db.transaction() as conn:
        count = conn.execute("SELECT count(*) AS n FROM fleet_boot_offers").fetchone()["n"]
    assert count == 1


def test_legacy_health_and_auto_promotion_never_qualify_new_fleet_baseline(registry) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO app_release_policy(singleton,promoted_tag,promoted_by) "
                     "VALUES(TRUE,%s,'auto')", (TAG,))
    assert service.status()["fleet_policy"]["source"] == "legacy_promotion"
    assert service.status()["base_baseline"]["source"] == "unconfigured"
    with pytest.raises(FleetError, match="recovery_required"):
        service.create_offer(_request(1, "a"))

    baseline = service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    offer = service.create_offer(_request(1, "a"))
    assert offer["app_policy_source"] == "legacy_promotion"
    assert offer["base_policy_source"] == "operator_baseline"
    with registry.db.transaction() as conn:
        conn.execute("UPDATE devices SET last_served_tag=%s,last_served_at=%s "
                     "WHERE serial=%s", (TAG, registry.clock.utc(), SERIAL))
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        assert record_base_health(
            conn, device_id,
            BaseHealth(authority_epoch=1, sequence=1, running_tag=TAG, healthy=True),
            clock=registry.clock,
        )
    view = service.status()
    assert view["base_baseline"] == {"revision": baseline["revision"],
                                      "tag": TAG, "source": "operator"}
    assert view["fleet_policy"]["source"] == "legacy_promotion"
    assert next(d for d in view["devices"] if d["serial"] == SERIAL)["base"]["state"] == (
        "legacy_tag_only")
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_accepted_artifacts").fetchone()[
            "n"] == 0
    with pytest.raises(psycopg.errors.CheckViolation):
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO fleet_accepted_artifacts(device_id,kind,content_key,"
                         "sha256,size,trust_mode,evidence_ref,accepted_at) "
                         "VALUES(%s,'base',%s,%s,%s,'t0','legacy-health',%s)",
                         (device_id, TARBALL_SHA, BASE_SHA, len(BASE_BYTES),
                          registry.clock.utc()))

    # An explicit empty fleet policy is a real selection, not a request to resume auto-promotion.
    service.set_app_policy(PolicyWrite(expected_revision=0, target=None))
    assert service.status()["fleet_policy"] == {"source": "explicit", "revision":
                                                  baseline["revision"] + 1, "target": None}
    next_offer = service.create_offer(_request(2, "b"))
    assert next_offer["app_policy_source"] == "explicit"
    assert next_offer["initial_app"] is None
