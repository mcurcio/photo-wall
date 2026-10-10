"""Migration 070 drops Central's V1 check-ins, fleet status and Player `.deb` delivery (decision
0019), and the real app then answers none of their routes while node boot and the node release
sync keep working.

One database at 069, seeded with a row of every V1 kind 070 drops (a check-in observation, an app
attempt, a Player `.deb` with its release row, reference and fetch outcome), is migrated to head
by `Database.migrate()`. The real `create_app` then serves it with node control on: a new serial's
boot offer creates its device and is charged to its daily offer quota (the functions moved out of
the V1 fleet service), and the quota's last offer is followed by a refusal. A release as the
packager writes it (`tests/support/release_build.py`) is ingested by the real sync. Real
PostgreSQL; skips without PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from content_db import schema_before
from fastapi.testclient import TestClient
from support.release_build import EPOCH, IMAGE_REFERENCES, REVISION, base_bundle, node_components
from test_content_catalog_sync import make_world
from test_fleet_attempts import OFFER_ID
from test_node_boot import cold_setup
from test_node_release_ingest import Upstream
from test_registry import ADMIN

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.fleet.node_sessions import NodeControlConfig
from central.kernel.job_types import SyncReleases
from central.kernel.ports import ReleaseListing
from central.registry import Registry
from contracts.node_boot import NodeBootRequestV2, encode_node_boot_request
from contracts.node_release import NODE_RELEASE_MANIFEST
from contracts.time import ManualClock
from scripts.package_release_artifacts import package

DEB_SHA = "d" * 64
V1_SERIAL = "abcdef0000000001"
V1_DEVICE = device_id_for_serial(V1_SERIAL)
V1_TAG = "v1.0.0"
DROPPED = ("fleet_os_observations", "fleet_app_attempts", "fleet_os_command_sessions",
           "fleet_os_attempt_reports", "fleet_app_fences", "fleet_accepted_artifacts",
           "fleet_artifact_abi", "fleet_generation_acceptances",
           "fleet_generation_current_app_attempts",
           "fleet_generation_current_os_command_sessions", "app_releases", "app_release_policy",
           "app_packages", "app_package_policy")
KEPT = ("app_release_poll", "devices", "fleet_device_lifecycle", "fleet_effect_gate",
        "fleet_t0_daily_quotas", "node_boot_offers", "node_deployments")
DELETED_ROUTES = (
    ("GET", "/v1/app/manifest"),
    ("GET", f"/v1/app/package/{DEB_SHA}.deb"),
    ("GET", "/v1/operator/app/releases"),
    ("POST", f"/v1/operator/app/releases/{V1_TAG}/promote"),
    ("POST", "/v1/appliance/check-ins"),
    ("POST", "/v2/appliance/check-ins"),
    ("GET", "/v1/operator/fleet"),
)
OFFER_DEVICE_DAILY = 128  # node_boot's per-device offer quota


@pytest.fixture
def before_070(empty_database):
    with schema_before(empty_database, "070") as db:
        yield Registry(db, ManualClock(1000))


def _seed_v1(conn) -> None:
    """A row of every V1 kind 070 drops."""
    conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                 "VALUES(%s,%s,900,900)", (V1_DEVICE, V1_SERIAL))
    conn.execute("INSERT INTO fleet_os_observations(device_id,kernel_boot_id,agent_incarnation,"
                 "observation_sequence,phase,received_at,observation_schema) "
                 "VALUES(%s,%s,'agent-1',1,'base_ready',900,1)", (V1_DEVICE, uuid4()))
    conn.execute("INSERT INTO fleet_app_attempts(attempt_id,device_id,offer_id,desired_revision,"
                 "target_sha256,phase,created_at,updated_at,device_generation) "
                 "VALUES(%s,%s,%s,1,%s,'queued',900,900,1)",
                 (UUID(int=43), V1_DEVICE, OFFER_ID, DEB_SHA))
    conn.execute("INSERT INTO app_releases(tag,major,minor,patch,is_prerelease,discovered_at,"
                 "updated_at,asset_url,asset_sha256,asset_size) "
                 "VALUES(%s,1,0,0,FALSE,900,900,'https://example.test/a.deb',%s,100)",
                 (V1_TAG, DEB_SHA))
    conn.execute("INSERT INTO app_release_policy(singleton,promoted_tag,promoted_by) "
                 "VALUES(TRUE,%s,'operator')", (V1_TAG,))
    conn.execute("INSERT INTO assets(kind,identity,created_at) VALUES('player-deb',%s,900)",
                 (DEB_SHA,))
    conn.execute("INSERT INTO asset_references(kind,identity,owner,locator_url,locator_sha256,"
                 "locator_size,expected_sha256,expected_size,added_at) "
                 "VALUES('player-deb',%s,%s,'https://example.test/a.deb',%s,100,%s,100,900)",
                 (DEB_SHA, V1_TAG, DEB_SHA, DEB_SHA))
    conn.execute("INSERT INTO job_outcomes(lock_key,job_name,status,reason,seq,updated_at) "
                 "VALUES(%s,'player_deb.fetch','terminal','origin_rejected',1,900)",
                 ("player-deb:" + DEB_SHA,))


def _one(registry, sql: str, *params):
    with registry.db.transaction() as conn:
        return conn.execute(sql, params).fetchone()


def _offer(client, serial: str):
    body = encode_node_boot_request(NodeBootRequestV2(serial, uuid4(), uuid4().hex + uuid4().hex))
    return client.post("/v2/node/boot-offers", content=body)


def _quota(registry, scope: str) -> int | None:
    row = _one(registry, "SELECT used FROM fleet_t0_daily_quotas "
                         "WHERE scope=%s AND kind='offer' AND day=0", scope)
    return None if row is None else row["used"]


def test_070_drops_the_v1_fleet_and_node_boot_keeps_its_quota(before_070, tmp_path):
    registry = before_070
    with registry.db.transaction() as conn:
        _seed_v1(conn)

    registry.db.migrate()

    for name in DROPPED:
        assert _one(registry, "SELECT to_regclass(%s) AS name", name)["name"] is None, name
    for name in KEPT:
        assert _one(registry, "SELECT to_regclass(%s) AS name", name)["name"] is not None, name
    assert _one(registry, "SELECT count(*) AS n FROM assets WHERE kind='player-deb'")["n"] == 0
    assert _one(registry, "SELECT count(*) AS n FROM job_outcomes "
                          "WHERE job_name='player_deb.fetch'")["n"] == 0

    cold_setup(registry)  # a selected node deployment, as the release sync and operator make it
    app = create_app(registry.db, registry.clock, ADMIN, media_root=tmp_path / "media",
                     node_control=NodeControlConfig("v1-fleet-test"))
    auth = {"Authorization": "Bearer " + ADMIN}
    with TestClient(app) as client:
        for method, path in DELETED_ROUTES:
            assert client.request(method, path, headers=auth, json={}).status_code == 404, path

        # A serial never seen: its offer creates the device and is charged to its daily quota.
        fresh = "20000000feed0001"
        fresh_id = device_id_for_serial(fresh)
        assert _one(registry, "SELECT 1 FROM devices WHERE device_id=%s", fresh_id) is None
        assert _offer(client, fresh).status_code == 200
        assert _one(registry, "SELECT serial FROM devices WHERE device_id=%s",
                    fresh_id) == {"serial": fresh}
        assert _quota(registry, fresh_id) == 1
        assert _one(registry, "SELECT used FROM fleet_t0_daily_quotas WHERE scope='global' "
                              "AND kind='new_device' AND day=0")["used"] == 1

        # One offer short of the limit: the last one is served, the next refused until tomorrow.
        busy = "20000000feed0002"
        busy_id = device_id_for_serial(busy)
        with registry.db.transaction() as conn:
            conn.execute("INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
                         "VALUES(%s,'offer',0,%s)", (busy_id, OFFER_DEVICE_DAILY - 1))
        assert _offer(client, busy).status_code == 200
        assert _quota(registry, busy_id) == OFFER_DEVICE_DAILY
        refused = _offer(client, busy)
        assert refused.status_code == 429 and refused.json() == {"error": "t0_rate_limited"}
        assert refused.headers["Retry-After"] == str(86400 - 1000)  # the day's end
        assert _quota(registry, busy_id) == OFFER_DEVICE_DAILY


def test_the_node_release_sync_ingests_a_packaged_release_with_no_v1_claim(registry, tmp_path):
    tag = "v2.0.0"
    destination = tmp_path / "release"
    package(base_bundle(tmp_path), node_components(tmp_path), destination, revision=REVISION,
            tag=tag, images=IMAGE_REFERENCES, source_date_epoch=EPOCH)
    files = {path.name: path.read_bytes() for path in destination.iterdir() if path.is_file()}
    manifest = files.pop(NODE_RELEASE_MANIFEST)
    upstream = Upstream()
    upstream.put(SimpleNamespace(tag=tag, files=files, manifest=manifest))
    world = make_world(registry, tmp_path / "cache")(
        ReleaseListing((), None, unchanged=False), origin=upstream.origin())

    asyncio.run(world.handler.handle(SyncReleases()))

    observed = _rows(registry, "SELECT tag, problem, manifest_sha256 FROM node_release_observations")
    assert [(row["tag"], row["problem"]) for row in observed] == [(tag, None)]
    assert observed[0]["manifest_sha256"] is not None
    [deployment] = _rows(registry, "SELECT deployment_id FROM node_deployments")
    assert _one(registry, "SELECT to_regclass('app_releases') AS name")["name"] is None
    owners = {row["owner"] for row in _rows(registry, "SELECT owner FROM asset_references")}
    assert owners == {"node-deployment:" + deployment["deployment_id"].hex}  # no release-tag claim


def _rows(registry, sql: str) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute(sql).fetchall()
