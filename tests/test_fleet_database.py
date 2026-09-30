"""Real PostgreSQL gates for migration 036 and immutable T0 transactions.

The shared registry fixture supplies a private migrated schema and skips without
PHOTO_WALL_TEST_DATABASE_URL; this test must run against local Compose and CI PostgreSQL.
"""

import hashlib
import os
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from central.assets.reader import Opened
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
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
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
