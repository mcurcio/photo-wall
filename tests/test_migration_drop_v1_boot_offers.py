"""Migration 069 drops Central's V1 boot offers, fleet intent and Player payload (decision 0019),
and the real app then answers none of the V1 routes and reads no V1 file from a release.

One database at 068, seeded with a row of every V1 kind 069 drops beside a real node boot (its
deployment, offer, admission and sessions, through Central's own boot service), is migrated to
head by `Database.migrate()`. The real `create_app` then serves it. A release as published before
the V1 files left the build (today's packager output from `tests/support/release_build.py` plus
the Player `.deb`, the bootstrapper `.deb`, the payload and `manifest.v2.json`) is ingested by the
real sync next to its node manifest. Real PostgreSQL; skips without
PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import UUID, uuid4

import psycopg
import pytest
from content_db import schema_before
from fastapi.testclient import TestClient
from support.release_build import EPOCH, IMAGE_REFERENCES, REVISION, node_components
from support.release_build import base_bundle as synthetic_base_bundle
from test_content_catalog_sync import make_world
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import claim_for, cold_setup
from test_node_release_ingest import Upstream, node_upload
from test_registry import ADMIN

from central.app import create_app
from central.kernel.job_types import SyncReleases
from central.kernel.ports import ReleaseListing
from central.registry import Registry
from contracts.node_boot import NodeBootRequestV2
from contracts.time import ManualClock
from scripts.package_release_artifacts import package

V1_OFFER = UUID(int=0x69)
PAYLOAD_SHA = "c" * 64
V1_TAG = "v1.0.0"  # cold_setup's base release
DROPPED_TABLES = ("fleet_boot_offers", "fleet_offer_artifact_roots", "fleet_app_policy",
                  "fleet_device_app_overrides", "fleet_base_policy", "fleet_maintenance_requests",
                  "device_base_health", "base_cache", "base_boot_status",
                  "fleet_artifact_retention_attempts")
DROPPED_COLUMNS = {
    "devices": ("attached_tag", "known_good_tag", "known_good_at", "last_served_tag",
                "last_served_at", "boot_outcome", "failed_tag"),
    # app_releases' payload_* and base_abi_* columns went too; 070 drops the table itself.
    "node_offer_contexts": ("legacy_offer_id",),
}
DELETED_ROUTES = (
    ("POST", "/v1/netboot/offers"),
    ("GET", f"/v1/netboot/offers/{V1_OFFER}/base"),
    ("GET", f"/v1/netboot/offers/{V1_OFFER}/app"),
    ("GET", "/v1/netboot/base"),
    ("GET", "/v1/netboot/manifest"),
    ("PUT", "/v1/operator/fleet/app-policy"),
    ("PUT", f"/v1/operator/fleet/devices/{DEVICE_ID}/app-override"),
    ("DELETE", f"/v1/operator/fleet/devices/{DEVICE_ID}/app-override"),
    ("PUT", "/v1/operator/fleet/base-baseline"),
    ("POST", f"/v1/operator/fleet/devices/{DEVICE_ID}/maintenance-requests"),
    ("DELETE", f"/v1/operator/fleet/devices/{DEVICE_ID}/maintenance-requests/{uuid4()}"),
    ("PUT", f"/v1/operator/devices/{DEVICE_ID}/pin"),
    ("DELETE", f"/v1/operator/devices/{DEVICE_ID}/pin"),
    ("GET", "/v1/operator/netboot"),
    ("POST", "/v1/player/base-health"),
)


@pytest.fixture
def before_069(empty_database):
    with schema_before(empty_database, "069") as db:
        yield Registry(db, ManualClock(1000))


def _seed_v1(conn) -> None:
    """A row of every V1 kind 069 drops, against the device the node boot uses."""
    conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,discovered_at,"
                 "updated_at,base_tarball_sha256) VALUES(%s,1,0,0,FALSE,900,900,%s)",
                 (V1_TAG, "b" * 64))
    conn.execute("UPDATE app_releases SET payload_url='https://example.test/p.tar.gz',"
                 "payload_sha256=%s,payload_size=100,payload_format='pw-player-data-v1',"
                 "payload_base_abi=%s,payload_source_manifest='manifest.v2.json',base_abi=%s,"
                 "base_abi_squashfs_sha256=%s,base_abi_source_manifest='manifest.v2.json' "
                 "WHERE tag=%s", (PAYLOAD_SHA, "sha256:" + "a" * 64, "sha256:" + "a" * 64,
                                  "d" * 64, V1_TAG))
    conn.execute("UPDATE devices SET attached_tag=%s,known_good_tag=%s,known_good_at=900,"
                 "last_served_tag=%s,last_served_at=900,boot_outcome='healthy' "
                 "WHERE device_id=%s", (V1_TAG, V1_TAG, V1_TAG, DEVICE_ID))
    conn.execute("INSERT INTO device_base_health(device_id,authority_epoch,sequence) "
                 "VALUES(%s,1,1)", (DEVICE_ID,))
    conn.execute("INSERT INTO base_cache(tag,state,updated_at) VALUES(%s,'cached',900)",
                 (V1_TAG,))
    conn.execute("INSERT INTO fleet_boot_offers(offer_id,installation_audience,device_id,serial,"
                 "kernel_boot_id,boot_nonce,base_policy_source,base_policy_revision,"
                 "app_policy_source,app_policy_revision,base_tag,base_content_key,base_sha256,"
                 "base_size,app_tag,app_sha256,app_size,app_status,compatibility_basis,"
                 "app_abi_key,offer_schema,app_format,app_base_abi,created_at,expires_at) "
                 "VALUES(%s,'photo-wall-central-t0',%s,%s,%s,%s,'operator_baseline',1,"
                 "'explicit',1,%s,%s,%s,1024,%s,%s,100,'selected','abi_match',%s,2,"
                 "'pw-player-data-v1',%s,900,2000)",
                 (V1_OFFER, DEVICE_ID, SERIAL, uuid4(), "1" * 32, V1_TAG, "b" * 64, "d" * 64,
                  V1_TAG, PAYLOAD_SHA, "sha256:" + "a" * 64, "sha256:" + "a" * 64))
    conn.execute("INSERT INTO fleet_offer_artifact_roots(offer_id,kind,content_key,sha256,size,"
                 "retain_until) VALUES(%s,'app',%s,%s,100,2000)",
                 (V1_OFFER, PAYLOAD_SHA, PAYLOAD_SHA))
    conn.execute("INSERT INTO fleet_app_policy(singleton,revision,target_tag,target_sha256,"
                 "target_size,target_format,changed_at) "
                 "VALUES(TRUE,1,%s,%s,100,'pw-player-data-v1',900)", (V1_TAG, PAYLOAD_SHA))
    conn.execute("INSERT INTO fleet_device_app_overrides(device_id,revision,target_tag,"
                 "target_sha256,target_size,target_format,changed_at) "
                 "VALUES(%s,2,%s,%s,100,'pw-player-data-v1',900)",
                 (DEVICE_ID, V1_TAG, PAYLOAD_SHA))
    conn.execute("INSERT INTO fleet_base_policy(singleton,revision,tag,source,changed_at) "
                 "VALUES(TRUE,3,%s,'operator',900)", (V1_TAG,))
    conn.execute("INSERT INTO fleet_maintenance_requests(request_id,device_id,device_generation,"
                 "status,policy_source,policy_revision,target_tag,target_sha256,target_size,"
                 "target_format,target_base_abi,target_source_manifest,ttl_seconds,"
                 "requested_at,expires_at,changed_at,reason) "
                 "VALUES(%s,%s,1,'queued','explicit',1,%s,%s,100,'pw-player-data-v1',%s,"
                 "'manifest.v2.json',300,900,1200,900,'operator_request')",
                 (uuid4(), DEVICE_ID, V1_TAG, PAYLOAD_SHA, "sha256:" + "a" * 64))
    conn.execute("INSERT INTO assets(kind,identity,created_at) "
                 "VALUES('player-payload',%s,900)", (PAYLOAD_SHA,))
    for owner in (V1_TAG, f"fleet-offer:{V1_OFFER}"):
        conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,"
                     "locator_sha256,locator_size,expected_sha256,expected_size,added_at) "
                     "VALUES('player-payload',%s,%s,'https://example.test/p.tar.gz',%s,100,"
                     "%s,100,900)", (PAYLOAD_SHA, owner, PAYLOAD_SHA, PAYLOAD_SHA))
    # The retired legacy adoption: a node session's context naming the V1 offer.
    conn.execute("INSERT INTO node_offer_contexts(offer_id,basis,legacy_offer_id) "
                 "VALUES(%s,'legacy_adoption',%s)", (V1_OFFER, V1_OFFER))


def _rows(registry, sql: str) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute(sql).fetchall()


def _node_state(registry) -> dict:
    return {table: _rows(registry, f"SELECT * FROM {table} ORDER BY 1")  # test-controlled names
            for table in ("node_boot_offers", "node_boot_admissions", "node_sessions",
                          "node_deployments", "node_boot_policy")}


def test_069_drops_the_v1_lane_and_keeps_every_node_record(before_069, tmp_path):
    registry = before_069
    boots, sessions, _ = cold_setup(registry)
    offer = boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    sessions.enroll(claim_for(offer))
    with registry.db.transaction() as conn:
        _seed_v1(conn)
    nodes = _node_state(registry)
    assert nodes["node_boot_offers"] and nodes["node_sessions"] and nodes["node_deployments"]

    registry.db.migrate()

    for table in DROPPED_TABLES:
        assert _rows(registry, f"SELECT to_regclass('{table}') AS name")[0]["name"] is None, table
    for table, columns in DROPPED_COLUMNS.items():
        present = {row["column_name"] for row in _rows(
            registry, "SELECT column_name FROM information_schema.columns "
                      f"WHERE table_schema=current_schema() AND table_name='{table}'")}
        assert present and not present & set(columns), (table, present & set(columns))
    assert _node_state(registry) == nodes  # every node record intact
    contexts = {row["offer_id"]: row for row in _rows(registry, "SELECT * FROM node_offer_contexts")}
    assert contexts[V1_OFFER] == {"offer_id": V1_OFFER, "basis": "legacy_adoption",
                                  "node_offer_id": None}  # history, without the V1 offer
    assert contexts[offer.offer_id]["basis"] == "node_v2"
    assert _rows(registry, "SELECT * FROM assets WHERE kind='player-payload'") == []
    assert _rows(registry, "SELECT * FROM asset_references WHERE kind='player-payload'") == []
    with pytest.raises(psycopg.errors.CheckViolation), registry.db.transaction() as conn:
        conn.execute("INSERT INTO assets(kind,identity,created_at) "
                     "VALUES('player-payload',%s,1)", (PAYLOAD_SHA,))

    app = create_app(registry.db, registry.clock, ADMIN, media_root=tmp_path / "media")
    auth = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        for method, path in DELETED_ROUTES:
            assert client.request(method, path, headers=auth, json={}).status_code == 404, path
        status = client.get("/v1/operator/node/status", headers=auth)
        assert status.status_code == 200  # the app serves; the V1 fleet routes are 070's


def _release_files(tmp_path, tag: str) -> dict[str, bytes]:
    """A release as the packager wrote it before the V1 files left the build: manifest.json
    naming the Player and bootstrapper `.deb`s beside the base and boot tarballs,
    manifest.v2.json naming the payload, those three files and the two tarballs (today's
    packager writes the tarballs; the V1 records and files are added here as they were)."""
    destination = tmp_path / "release"
    package(synthetic_base_bundle(tmp_path), node_components(tmp_path), destination,
            revision=REVISION, tag=tag, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    files = {manifest[key]["filename"]: (destination / manifest[key]["filename"]).read_bytes()
             for key in ("base_image", "boot_image")}
    for key, name in (("player_deb", "photo-wall-player_0.1.0+gdeadbeef_arm64.deb"),
                      ("bootstrapper_deb", "photo-wall-bootstrapper_0.1.0+gdeadbeef_arm64.deb"),
                      ("player_payload", f"photo-wall-player-payload-{REVISION}.tar.gz")):
        data = f"fake {key} bytes".encode() * 100
        files[name] = data
        manifest[key] = {"filename": name, "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data)}
    payload = manifest.pop("player_payload")
    files["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    files["manifest.v2.json"] = json.dumps({**manifest, "schema": 2,
                                            "player_payload": payload}).encode()
    return files


def test_a_release_still_carrying_v1_files_lands_its_node_release_and_no_v1_asset(
        registry, tmp_path):
    tag = "v2.0.0"
    upstream, upload = Upstream(), node_upload(tag)
    upstream.put(upload)
    for name, data in _release_files(tmp_path, tag).items():
        path = f"/{tag}/{name}"
        upstream.files[path] = data
        upstream.entries[tag]["assets"].append({
            "name": name, "browser_download_url": "https://assets.test" + path,
            "id": 10_000 + len(upstream.files), "updated_at": "2026-09-30T00:00:00Z"})
    world = make_world(registry, tmp_path / "cache")(
        ReleaseListing((), None, unchanged=False), origin=upstream.origin())
    asyncio.run(world.handler.handle(SyncReleases()))

    observed = _rows(registry, "SELECT tag, manifest_sha256, problem FROM node_release_observations")
    assert observed == [{"tag": tag, "manifest_sha256": upload.sha, "problem": None}]
    assert _rows(registry, "SELECT deployment_id FROM node_deployments") == [
        {"deployment_id": upload.deployment_id}]
    kinds = {row["kind"] for row in _rows(registry, "SELECT DISTINCT kind FROM assets")}
    assert kinds == {"os-image", "sealed-environment"}  # no 'player-deb', no 'player-payload'
