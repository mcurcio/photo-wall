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
from central.netboot_base import record_base_health
from contracts.models import BaseHealth

APP_SHA = "a" * 64
TARBALL_SHA = "b" * 64
BASE_BYTES = b"measured database route base"
BASE_SHA = hashlib.sha256(BASE_BYTES).hexdigest()
SERIAL = "abcdef1234567890"
TAG = "v1.2.3"


def _seed_release(registry) -> None:
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,asset_sha256,"
            "asset_size,mirror_state,discovered_at,updated_at,base_tarball_sha256) "
            "VALUES(%s,1,2,3,FALSE,%s,123,'mirrored',1,1,%s)",
            (TAG, APP_SHA, TARBALL_SHA),
        )
        conn.execute(
            "INSERT INTO base_cache(tag,squashfs_sha256,size,state,updated_at) "
            "VALUES(%s,%s,456,'cached',1)", (TAG, BASE_SHA),
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
    assert first["base"] == {"tag": TAG, "sha256": BASE_SHA, "size": 456}
    assert first["initial_app"] == {"tag": TAG, "sha256": APP_SHA, "size": 123}
    assert first["base_policy_revision"] == baseline["revision"]
    assert first["app_policy_revision"] == policy["revision"]
    assert first["app_policy_source"] == "explicit"
    assert first["compatibility_basis"] == "co_release_unverified"
    service.set_app_policy(PolicyWrite(expected_revision=policy["revision"], target=None))
    assert service.create_offer(_request(1, "a")) == first
    with registry.db.transaction() as conn:
        roots = conn.execute("SELECT kind,content_key,sha256,size "
                             "FROM fleet_offer_artifact_roots "
                             "WHERE offer_id=%s ORDER BY kind", (first["offer_id"],)).fetchall()
    assert roots == [{"kind": "app", "content_key": APP_SHA,
                      "sha256": APP_SHA, "size": 123},
                     {"kind": "base", "content_key": TARBALL_SHA,
                      "sha256": BASE_SHA, "size": 456}]
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


def test_mounted_offer_route_uses_migrated_selection_and_exact_open(registry, tmp_path) -> None:
    _seed_release(registry)
    service = FleetService(registry.db, registry.clock)
    service.set_base_baseline(BaselineWrite(expected_revision=0, tag=TAG))
    path = tmp_path / "base.squashfs"
    path.write_bytes(BASE_BYTES)

    class Reader:
        async def read(self, candidates):
            return Opened(candidates.jobs[0], os.open(path, os.O_RDONLY), len(BASE_BYTES),
                          "0" * 64)

    app = FastAPI()
    mount_fleet_routes(app, db=registry.db, clock=registry.clock, admin=lambda: None,
                       content=SimpleNamespace(reader=Reader()))
    client = TestClient(app)
    body = _request(1, "a").model_dump(by_alias=True, mode="json")
    first = client.post("/v1/netboot/offers", json=body)
    assert first.status_code == 200
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
        conn.execute("UPDATE devices SET last_served_tag=%s WHERE serial=%s", (TAG, SERIAL))
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
                         "VALUES(%s,'base',%s,%s,456,'t0','legacy-health',%s)",
                         (device_id, TARBALL_SHA, BASE_SHA, registry.clock.utc()))

    # An explicit empty fleet policy is a real selection, not a request to resume auto-promotion.
    service.set_app_policy(PolicyWrite(expected_revision=0, target=None))
    assert service.status()["fleet_policy"] == {"source": "explicit", "revision":
                                                  baseline["revision"] + 1, "target": None}
    next_offer = service.create_offer(_request(2, "b"))
    assert next_offer["app_policy_source"] == "explicit"
    assert next_offer["initial_app"] is None
