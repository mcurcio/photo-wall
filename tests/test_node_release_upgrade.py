"""Central reads the node documents an earlier Central stored (decision 0019, E-0019-FIX-16).

Before 0019 the manager root's package was `photo-wall-node-manager`; it is now
`photo-wall-app-manager`. Rows in `node_release_catalog`, `node_deployments` and
`node_boot_offers` are re-parsed on every read, and no migration rewrites them, so a package
naming rule judged when Central parses a stored document would refuse every release, deployment
and boot offer the earlier Central wrote. The rows here are written the way that Central wrote
them: by the same writers and encoders (unchanged since), run while the contract's manager
package was the earlier name, so the stored bytes name `photo-wall-node-manager`. Then the
current Central serves them through its real routes and its release sync.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BOOT_ID, SERIAL
from test_node_release_catalog import store_files
from test_node_release_ingest import (
    Upstream,
    make_sync_world,
    node_upload,
    observations,
    policy,
    sessions_for,
    sync,
)
from test_registry import ADMIN

import contracts.node_boot
from central.app import create_app
from central.fleet.node_boot import NodeBootService
from central.fleet.node_sessions import NodeControlConfig
from contracts.node_boot import NodeBootRequestV2, parse_node_boot_offer
from contracts.node_commands import NodeSessionClaim, encode_session_claim

EARLIER_MANAGER = "photo-wall-node-manager"  # the manager root's package before decision 0019
HEADERS = {"Authorization": "Bearer " + ADMIN}


@pytest.fixture
def sync_world(registry, tmp_path):
    return make_sync_world(registry, tmp_path)


def _as_earlier_central(monkeypatch):
    """The contract as the earlier Central held it: its manager package was EARLIER_MANAGER."""
    monkeypatch.setattr(contracts.node_boot, "MANAGER_PACKAGE", EARLIER_MANAGER)


def _published(sync_world):
    """One release whose manager root names EARLIER_MANAGER, on a mock upstream: (upload, world)."""
    upstream, upload = Upstream(), node_upload("v2.0.0", manager_package=EARLIER_MANAGER)
    upstream.put(upload)
    return upload, sync_world(upstream)


def _client(registry):
    return TestClient(create_app(registry.db, registry.clock, ADMIN,
                                 node_control=NodeControlConfig("node-test")))


def _boot(client, request: NodeBootRequestV2):
    from contracts.node_boot import encode_node_boot_request
    return client.post("/v2/node/boot-offers", content=encode_node_boot_request(request))


def test_a_release_the_earlier_central_ingested_is_listed_auto_selected_and_offered(
        registry, sync_world, monkeypatch):
    with monkeypatch.context() as earlier:
        _as_earlier_central(earlier)
        upload, world = _published(sync_world)
        sync(world)  # the earlier Central's ingest: catalog row, observation, deployment
    assert observations(registry)["v2.0.0"]["manifest_sha256"] == upload.sha

    # The release sync's tail reads the stored catalog row and auto-selects its deployment.
    store_files(world, upload)
    sync(world)
    assert observations(registry)["v2.0.0"]["problem"] is None
    assert policy(registry)["deployment_id"] == upload.deployment_id

    with _client(registry) as client:
        page = client.get("/v1/operator/node/releases", headers=HEADERS)
        assert page.status_code == 200, page.text
        assert [(row["tag"], row["deployment_id"]) for row in page.json()["releases"]] == [
            ("v2.0.0", str(upload.deployment_id))]
        response = _boot(client, NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
        assert response.status_code == 200, response.text
    offer = parse_node_boot_offer(response.content)
    assert offer.manager_primary.deb_name == EARLIER_MANAGER
    assert offer.app_environment == upload.release.app_environment


def test_an_offer_the_earlier_central_stored_replays_enrolls_and_serves_its_artifacts(
        registry, sync_world, monkeypatch):
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    boots = NodeBootService(sessions_for(registry))
    with monkeypatch.context() as earlier:
        _as_earlier_central(earlier)
        upload, world = _published(sync_world)
        sync(world)
        boots.select(upload.deployment_id, 0)
        boots.offer(request)  # stored in node_boot_offers
    with registry.db.transaction() as conn:
        stored = bytes(conn.execute("SELECT offer_payload FROM node_boot_offers").fetchone()[
            "offer_payload"])
    assert EARLIER_MANAGER.encode() in stored

    with _client(registry) as client:
        replay = _boot(client, request)  # the same boot asks again: the stored offer replays
        assert replay.status_code == 200, replay.text
        assert replay.content == stored
        offer = parse_node_boot_offer(stored)
        claim = NodeSessionClaim(SERIAL, offer.offer_id, BOOT_ID, "host_core", UUID(int=1),
                                 UUID(int=2), "d" * 64)
        enrolled = client.post("/v2/node/sessions", content=encode_session_claim(claim))
        assert enrolled.status_code == 200, enrolled.text
        fresh = _boot(client, NodeBootRequestV2(SERIAL, UUID(int=902), "b" * 64))
        assert fresh.status_code == 200, fresh.text  # a new boot: the stored deployment's offer
        page = client.get("/v1/operator/node/releases", headers=HEADERS)
        assert page.status_code == 200, page.text
        assert page.json()["selection"]["deployment_id"] == str(upload.deployment_id)
    asset = boots.asset(offer.offer_id, "manager-primary")
    assert asset.sha256 == offer.manager_primary.environment_sha256
