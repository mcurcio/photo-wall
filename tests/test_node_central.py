"""The node routes Central mounts, with real PostgreSQL: the boot offer stays; every old-path route
(sessions, app links, commands, app operations, the posts behind no page) is gone (decision 0034, S4c)."""
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_registry import ADMIN

from central.app import create_app
from contracts.node_boot import NodeBootRequestV2, encode_node_boot_request


def test_the_default_app_serves_the_boot_offer_and_no_old_path(registry):
    """Node control is not a mode: the default composition serves the boot offer (without a cache
    no release is ready, so it refuses `node_boot_policy_unconfigured`), never cached. The host
    polls for no command (an operator's reboot goes over the bus, tests/test_operator_reboot.py)
    and the Pi enrolls no session and proves no app link (S4c-2a-v).
    Mutation probe: re-mount `/v2/node/sessions` -> its post answers, fails."""
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        offered = client.post("/v2/node/boot-offers",
                              content=encode_node_boot_request(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)))
        assert (offered.status_code, offered.json()) == (503, {"error": "node_boot_policy_unconfigured"})
        assert offered.headers["cache-control"] == "private, no-store"
        headers = {"Authorization": "Bearer " + "d" * 64, "X-Node-Session": str(UUID(int=3))}
        for path in ("/v2/node/sessions", "/v2/node/app-links"):
            assert client.post(path, content=b"{}", headers=headers).status_code in (404, 405), path
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
