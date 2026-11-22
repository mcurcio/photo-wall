"""Every route the operator session authorizes lives under OPERATOR_PREFIX, the cookie's path,
and every route under it asks for a permission (decision 0035), in token mode and in provider
mode alike.

No database: the app is constructed, never served.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from operator_provider_fake import FakeProvider

from central.app import create_app
from central.db import Database
from central.operator_auth import COOKIE_PATH, OPERATOR_PREFIX, OperatorAuth
from contracts.time import ManualClock

ADMIN = "test-operator-" + "x" * 40


def _guarded(dependant, call) -> bool:
    return any(sub.call == call or _guarded(sub, call) for sub in dependant.dependencies)


def _app(mode: str) -> FastAPI:
    db = Database("postgresql://unused@127.0.0.1:1/unused", application_name="photo-wall-test")
    if mode == "token":
        return create_app(db, ManualClock(0), ADMIN, run_scheduler=False)
    fake = FakeProvider()
    return create_app(db, ManualClock(0), ADMIN if mode == "provider+break-glass" else None,
                      run_scheduler=False, oidc=fake.settings(), identity_provider=fake)


def _asks_a_permission(app: FastAPI, route: APIRoute) -> bool:
    auth = app.state.operator_auth
    return any(_guarded(route.dependant, guard) for guard in (auth.admin, auth.operate))


def _admin_routes(app: FastAPI) -> set[str]:
    admin = app.state.operator_auth.admin
    return {route.path for route in app.routes
            if isinstance(route, APIRoute) and _guarded(route.dependant, admin)}


def test_every_admin_route_of_the_real_app_is_under_the_cookie_path():
    app = create_app(Database("postgresql://unused@127.0.0.1:1/unused", application_name="photo-wall-test"), ManualClock(0), ADMIN,
                     run_scheduler=False)
    routes = _admin_routes(app)
    # Non-vacuous: console callers of node and calibration routes are found.
    assert {"/v1/operator/node/hosts", "/v1/operator/frames/{frame_id}/calibration-capability",
            "/v1/operator/app/releases/refresh"} <= routes
    assert COOKIE_PATH == OPERATOR_PREFIX
    assert sorted(path for path in routes if not path.startswith(OPERATOR_PREFIX)) == []


# The door itself: how to sign in, signing in and signing out are the only operator routes that
# ask for no permission (they are how one is got, or given up).
SESSION_ROUTES = {("POST", "/v1/operator/session"), ("DELETE", "/v1/operator/session"),
                  ("GET", "/v1/operator/sign-in")}
# Provider mode adds the code flow's two navigations and the provider's own call; its POST
# session only answers 404.
PROVIDER_SESSION_ROUTES = SESSION_ROUTES | {
    ("GET", "/v1/operator/session/start"), ("GET", "/v1/operator/session/callback"),
    ("POST", "/v1/operator/session/backchannel-logout")}
OPEN_ROUTES = {"token": SESSION_ROUTES, "provider": PROVIDER_SESSION_ROUTES,
               "provider+break-glass": PROVIDER_SESSION_ROUTES}


@pytest.mark.parametrize("mode", sorted(OPEN_ROUTES))
def test_every_route_under_the_operator_prefix_asks_for_a_permission_and_none_outside_it_does(mode):
    """Per method and path, in each mode: the open routes are exactly the door's, and no route
    outside the prefix depends on `admin` or `operate`."""
    app = _app(mode)
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    inside = {(method, route.path): _asks_a_permission(app, route) for route in routes
              if route.path.startswith(OPERATOR_PREFIX) for method in route.methods}
    assert len(inside) > 40
    assert {key for key, asks in inside.items() if not asks} == OPEN_ROUTES[mode]
    # Who am I and People and access are read behind the login, in both modes.
    assert inside[("GET", "/v1/operator/session")] and inside[("GET", "/v1/operator/access")]
    outside = [route.path for route in routes
               if not route.path.startswith(OPERATOR_PREFIX) and _asks_a_permission(app, route)]
    assert outside == []
    assert len(routes) - len([r for r in routes if r.path.startswith(OPERATOR_PREFIX)]) > 10   # non-vacuous


def test_a_provider_without_a_break_glass_token_needs_no_admin_token_and_token_mode_needs_one(monkeypatch):
    monkeypatch.delenv("PHOTO_WALL_ADMIN_TOKEN", raising=False)
    for name in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "ROLE", "ROLES_CLAIM", "NAME"):
        monkeypatch.delenv("PHOTO_WALL_OIDC_" + name, raising=False)
    db = Database("postgresql://unused@127.0.0.1:1/unused", application_name="photo-wall-test")
    fake = FakeProvider()
    create_app(db, ManualClock(0), None, run_scheduler=False, oidc=fake.settings(), identity_provider=fake)
    with pytest.raises(KeyError, match="PHOTO_WALL_ADMIN_TOKEN"):
        create_app(db, ManualClock(0), None, run_scheduler=False)
    # A break-glass token is held to the same length as token mode's.
    with pytest.raises(ValueError, match="at least 32 characters"):
        create_app(db, ManualClock(0), "short", run_scheduler=False, oidc=fake.settings(),
                   identity_provider=fake)
    monkeypatch.setenv("PHOTO_WALL_ADMIN_TOKEN", "short")
    with pytest.raises(ValueError, match="at least 32 characters"):
        create_app(db, ManualClock(0), None, run_scheduler=False, oidc=fake.settings(), identity_provider=fake)


def test_the_environment_configures_the_provider_and_a_partial_setting_stops_central(monkeypatch):
    db = Database("postgresql://unused@127.0.0.1:1/unused", application_name="photo-wall-test")
    monkeypatch.setenv("PHOTO_WALL_OIDC_ISSUER", "https://id.example/realms/home")
    monkeypatch.delenv("PHOTO_WALL_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("PHOTO_WALL_OIDC_CLIENT_SECRET", raising=False)
    with pytest.raises(ValueError) as stopped:
        create_app(db, ManualClock(0), ADMIN, run_scheduler=False)
    assert str(stopped.value).endswith("PHOTO_WALL_OIDC_CLIENT_ID, PHOTO_WALL_OIDC_CLIENT_SECRET missing")
    monkeypatch.setenv("PHOTO_WALL_OIDC_CLIENT_ID", "photo-wall")
    monkeypatch.setenv("PHOTO_WALL_OIDC_CLIENT_SECRET", "s3cret-" + "s" * 30)
    # Fully set: provider mode, and the provider is not asked anything at Central's start.
    app = create_app(db, ManualClock(0), ADMIN, run_scheduler=False)
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/v1/operator/session/callback" in paths
    assert "operator-session-retention" in app.state.scheduler.retentions
    # Token mode keeps its one retention step: nothing of the provider's is wired.
    for name in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv("PHOTO_WALL_OIDC_" + name)
    token_mode = create_app(db, ManualClock(0), ADMIN, run_scheduler=False)
    assert list(token_mode.state.scheduler.retentions) == ["node-retention"]


def test_every_operator_route_of_the_real_app_requires_the_operator_login():
    """Per method and path, so one route losing its `admin` dependency (a GET beside a guarded
    POST) fails here, whichever slice added it."""
    app = create_app(Database("postgresql://unused@127.0.0.1:1/unused", application_name="photo-wall-test"), ManualClock(0), ADMIN,
                     run_scheduler=False)
    admin = app.state.operator_auth.admin
    open_routes = {(method, route.path) for route in app.routes
                   if isinstance(route, APIRoute) and route.path.startswith(OPERATOR_PREFIX)
                   and not _guarded(route.dependant, admin) for method in route.methods}
    assert open_routes == SESSION_ROUTES
    # Non-vacuous: the Power tab's routes are among the guarded ones.
    guarded = {(method, route.path) for route in app.routes
               if isinstance(route, APIRoute) and _guarded(route.dependant, admin) for method in route.methods}
    assert {("GET", "/v1/operator/frames/{frame_id}/power"),
            ("PUT", "/v1/operator/displays/{display_id}/power-settings")} <= guarded


def test_require_scoped_refuses_a_direct_or_nested_admin_route_outside_the_prefix():
    auth = OperatorAuth(ADMIN, ManualClock(0))

    def nested(_: None = Depends(auth.admin)) -> None:
        return None

    def nested_operate(_: None = Depends(auth.operate)) -> None:
        return None

    for dependency in (auth.admin, nested, auth.operate, nested_operate):
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
