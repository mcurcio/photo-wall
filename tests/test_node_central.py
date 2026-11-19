"""Durable V2 node admission and the node routes Central mounts, with real PostgreSQL and inert effects."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BOOT_ID, DEVICE_ID, OFFER_ID, SERIAL, _seed
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_sessions import (
    NODE_AUDIENCE,
    NodeControlConfig,
    NodeControlError,
    NodeSessions,
)
from contracts.node_commands import NodeSessionClaim, encode_session_claim, parse_session_grant


def node_offer(registry, config=None):
    """A node boot offer for BOOT_ID, through Central's own boot service; under `config`'s
    audience when given (the offer binds it), else cold_setup's."""
    from test_node_boot import cold_setup

    from central.fleet.node_boot import NodeBootService
    from contracts.node_boot import NodeBootRequestV2
    boots, sessions, _ = cold_setup(registry)
    if config is not None:
        sessions = NodeSessions(registry.db, registry.clock, config)
        boots = NodeBootService(sessions, versions=boots.versions)
    return sessions, boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))


def setup(registry, owner="host_core"):
    sessions, offer = node_offer(registry)
    claim = NodeSessionClaim(SERIAL, offer.offer_id, BOOT_ID, owner, uuid4(), uuid4(),
                             uuid4().hex + uuid4().hex)
    return sessions, claim, sessions.enroll(claim)


def test_a_claim_naming_no_node_boot_offer_is_refused(registry):
    """Central adopts no V1 boot offer: a claim must name a node boot offer."""
    _seed(registry)
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))
    claim = NodeSessionClaim(SERIAL, OFFER_ID, BOOT_ID, "host_core", uuid4(), uuid4(),
                             uuid4().hex + uuid4().hex)
    with pytest.raises(NodeControlError, match="node_offer_mismatch"):
        sessions.enroll(claim)


def test_session_exact_retry_concurrent_and_scope_separation(registry):
    sessions, claim, grant = setup(registry)
    with ThreadPoolExecutor(4) as pool:
        assert all(item == grant for item in pool.map(sessions.enroll, [claim] * 4))
    broker_claim = replace(claim, owner="app_effect_broker", session_id=uuid4(),
                           credential=uuid4().hex + uuid4().hex)
    broker = sessions.enroll(broker_claim)
    assert broker.scope == "app_effect" and grant.scope == "operator_reboot"
    with registry.db.transaction() as conn:
        with pytest.raises(NodeControlError, match="session_unavailable"):
            sessions.authenticate_in(conn, broker.session_id, claim.credential)
    with pytest.raises(NodeControlError, match="credential_reuse"):
        sessions.enroll(replace(claim, session_id=uuid4()))


def test_the_default_app_serves_node_routes_and_no_command_poll(registry):
    """Node control is not a mode: the default composition serves the node path, with nothing
    opened first (no effect gate, owner answer G-A). The host polls for no command: an operator's
    reboot goes over the bus (tests/test_operator_reboot.py), so `GET /v2/node/commands` is gone."""
    _, offer = node_offer(registry, NodeControlConfig())
    claim = NodeSessionClaim(SERIAL, offer.offer_id, BOOT_ID, "host_core", uuid4(), uuid4(),
                             "d" * 64)
    app = create_app(registry.db, registry.clock, ADMIN)
    assert app.state.node_sessions.config == NodeControlConfig(NODE_AUDIENCE)
    assert NODE_AUDIENCE == "photo-wall"
    with TestClient(app) as client:
        response = client.post("/v2/node/sessions", content=encode_session_claim(claim))
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        grant = parse_session_grant(response.content)
        headers = {"Authorization": "Bearer " + claim.credential, "X-Node-Session": str(grant.session_id)}
        assert grant.command_eligible and grant.command_reason == "boot_admitted"
        assert client.get("/v2/node/commands", headers=headers).status_code in (404, 405)
        admin = {"Authorization": "Bearer " + ADMIN}
        assert client.get("/v1/operator/node/status", headers=admin).status_code == 404


# The app operations' routes, gone with what the console showed of the Pi's old path (S4c-2a-iii).
APP_OPERATION_ROUTES = (
    ("POST", "/v2/node/app-preparation"), ("POST", "/v2/node/app-effects"),
    ("POST", "/v2/node/app-responses"), ("GET", "/v2/node/app-desired"), ("GET", "/v2/node/app-commands"),
    ("GET", f"/v2/node/app-attempts/{UUID(int=1)}/artifacts/root"),
    ("POST", f"/v1/operator/node/devices/{DEVICE_ID}/app-stages"),
    ("POST", f"/v1/operator/node/devices/{DEVICE_ID}/app-qualifications"),
    ("POST", f"/v1/operator/node/app-qualifications/{UUID(int=2)}/sample"),
    ("GET", f"/v1/operator/node/devices/{DEVICE_ID}/app-attempts"),
)


@pytest.mark.parametrize("method, path", APP_OPERATION_ROUTES)
def test_the_app_operation_routes_are_not_mounted(registry, method, path):
    """Mutation probe: re-mount one (the app-attempts read) -> its request answers, fails."""
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.request(method, path, content=b"{}", headers={"Authorization": "Bearer " + ADMIN})
        assert response.status_code in (404, 405), (method, path, response.status_code)


# The old path's posts behind no page, gone with the display exchange and the reconciler (S4c-2a-iv).
OLD_NODE_POSTS = ("/v2/node/evidence", "/v2/node/observations", "/v2/node/host-facts", "/v2/node/display")


@pytest.mark.parametrize("path", OLD_NODE_POSTS)
def test_the_old_node_posts_are_not_mounted(registry, path):
    """Mutation probe: re-mount `/v2/node/display` -> its post answers, fails."""
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.post(path, content=b"{}", headers={"Authorization": "Bearer x",
                                                            "X-Node-Session": str(UUID(int=3))})
        assert response.status_code in (404, 405), (path, response.status_code)
