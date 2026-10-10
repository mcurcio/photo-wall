"""Every route the operator session authorizes lives under OPERATOR_PREFIX, the cookie's path.

No database: the app is constructed, never served.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from central.app import create_app
from central.db import Database
from central.operator_auth import COOKIE_PATH, OPERATOR_PREFIX, OperatorAuth
from contracts.time import ManualClock

ADMIN = "test-operator-" + "x" * 40


def _guarded(dependant, call) -> bool:
    return any(sub.call == call or _guarded(sub, call) for sub in dependant.dependencies)


def _admin_routes(app: FastAPI) -> set[str]:
    admin = app.state.operator_auth.admin
    return {route.path for route in app.routes
            if isinstance(route, APIRoute) and _guarded(route.dependant, admin)}


def test_every_admin_route_of_the_real_app_is_under_the_cookie_path():
    app = create_app(Database("postgresql://unused@127.0.0.1:1/unused"), ManualClock(0), ADMIN,
                     run_scheduler=False, mdns_enabled=False)
    routes = _admin_routes(app)
    # Non-vacuous: console callers of node and calibration routes are found.
    assert {"/v1/operator/node/status", "/v1/operator/frames/{frame_id}/calibration-capability",
            "/v1/operator/app/releases/refresh"} <= routes
    assert COOKIE_PATH == OPERATOR_PREFIX
    assert sorted(path for path in routes if not path.startswith(OPERATOR_PREFIX)) == []


def test_require_scoped_refuses_a_direct_or_nested_admin_route_outside_the_prefix():
    auth = OperatorAuth(ADMIN, ManualClock(0))

    def nested(_: None = Depends(auth.admin)) -> None:
        return None

    for dependency in (auth.admin, nested):
        app = FastAPI()
        app.get(OPERATOR_PREFIX + "inside", dependencies=[Depends(dependency)])(lambda: None)
        auth.require_scoped(app)
        app.get("/v2/operator/outside", dependencies=[Depends(dependency)])(lambda: None)
        with pytest.raises(RuntimeError, match="/v2/operator/outside"):
            auth.require_scoped(app)


def test_prefix_headers_cover_an_unhandled_500_without_the_operator_mount():
    """`add_prefix_headers` creates its table and the unhandled-500 handler together, so an
    app that never mounts OperatorAuth still answers a crash under the prefix with its
    headers (Starlette's ServerErrorMiddleware runs outside every user middleware)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from central.operator_auth import add_prefix_headers

    app = FastAPI()
    add_prefix_headers(app, "/v1/fixed/", {"Cross-Origin-Resource-Policy": "same-origin"})

    @app.get("/v1/fixed/boom")
    def boom():
        raise RuntimeError("synthetic")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/fixed/boom")
    assert response.status_code == 500
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
