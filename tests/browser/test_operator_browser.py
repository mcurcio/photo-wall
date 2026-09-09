"""Real Chromium + production HTTP/UI + disposable PostgreSQL registry walkthrough.

Equipment is simulated. No worker, media rendering, PXE, or physical claims follow.
Opt in explicitly so ordinary portable runs do not need a downloaded browser.
"""

import json
import os
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from playwright.sync_api import expect
from test_registry import ADMIN

from central.app import create_app
from central.db import Database
from central.installation_models import InstallationInventory

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)


@contextmanager
def operator_server(db, clock, *, media_root=None, media_queue=None):
    """Run the production app on an ephemeral loopback listener with real lifespan."""
    app = create_app(db, clock, ADMIN, run_scheduler=False,
                     media_root=media_root, media_queue=media_queue)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, "operator server did not start"
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "operator server did not stop"


def connect(page, origin, token=ADMIN):
    page.goto(origin)
    page.get_by_label("Operator token").fill(token)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#controls")).to_be_visible()
    expect(page.locator("#message")).to_have_text("Connected.")


def command(page, name, endpoint, *, status=200):
    button = page.get_by_role("button", name=name, exact=True)
    with page.expect_response(lambda response: response.url.endswith(endpoint)
                             and response.request.method != "GET") as received:
        button.click()
    assert received.value.status == status
    expect(button).to_be_enabled()
    return received.value.json()


def inventory(page, origin):
    """Read-only proof through the complete public Installation contract."""
    response = page.request.get(origin + "/v1/operator/inventory", headers={
        "Authorization": "Bearer " + ADMIN,
    })
    assert response.status == 200
    return InstallationInventory.model_validate_json(response.body())


def create_frame(page, frame_id="browser-frame"):
    page.get_by_label("Frame name / ID").fill(frame_id)
    command(page, "Create Frame", "/v1/operator/frames", status=201)
    expect(page.locator("#frames")).to_contain_text(frame_id)


def bind(page, player_id, frame_id="browser-frame"):
    page.locator("#bind-frame").select_option(frame_id)
    page.locator("#bind-output").select_option(json.dumps([player_id, "HDMI-A-1"],
                                                        separators=(",", ":")))
    command(page, "Bind Output", f"/v1/operator/frames/{frame_id}/binding")
    expect(page.locator("#frames")).to_contain_text(player_id)


def test_authenticated_operator_registry_walkthrough_and_persistence(page, installation):
    registry, first, replacement = installation
    with operator_server(registry.db, registry.clock) as origin:
        page.goto(origin)
        expect(page.locator("#controls")).to_be_hidden()
        page.get_by_label("Operator token").fill("invalid-disposable-token")
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#message")).to_contain_text("token was not accepted")
        expect(page.locator("#login")).to_be_visible()
        expect(page.locator("#controls")).to_be_hidden()

        connect(page, origin)
        expect(page.get_by_label("Operator token")).to_have_value("")
        expect(page.locator("#players tbody tr")).to_have_count(2)
        expect(page.locator("#bind-output option")).to_have_count(3)
        create_frame(page)
        bind(page, first)
        calibration_path = "/v1/operator/frames/browser-frame/calibration"
        page.locator("#gain").fill("0.75")
        command(page, "Preview", calibration_path)
        expect(page.locator("#message")).to_have_text("Preview active for 30 seconds.")
        observed = inventory(page, origin).frames[0]
        assert observed.preview.gain == .75 and observed.calibration.gain == 1
        command(page, "Revert", calibration_path)
        expect(page.locator("#gain")).to_have_value("1")
        assert inventory(page, origin).frames[0].preview is None

        page.locator("#gain").fill("0.8")
        command(page, "Commit", calibration_path)
        expect(page.locator("#frames")).to_contain_text("Committed r2")
        original = inventory(page, origin).frames[0]
        assert original.calibration_valid and original.calibration.gain == .8

        bind(page, replacement)
        expect(page.locator("#frames")).to_contain_text("Review required")
        moved = inventory(page, origin).frames[0]
        assert moved.generation == original.generation + 1
        assert (moved.width_mm, moved.height_mm, moved.calibration) == (
            original.width_mm, original.height_mm, original.calibration,
        )
        assert not moved.calibration_valid
        command(page, "Commit", calibration_path)
        page.get_by_text("Retire a Player", exact=True).click()
        page.locator("#retire-player").select_option(first)
        command(page, "Retire selected Player", f"/v1/operator/players/{first}/retire")
        expect(page.locator("#players")).to_contain_text("Retired")
        expect(page.locator("#bind-output option")).to_have_count(1)
        saved = inventory(page, origin)
        assert saved.frames[0].player_id == replacement
        assert saved.frames[0].calibration_valid

        page.reload()
        expect(page.locator("#login")).to_be_visible()
        expect(page.locator("#controls")).to_be_hidden()
        expect(page.get_by_label("Operator token")).to_have_value("")
        assert page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0

    # A new server and connection pool reconstruct the result from PostgreSQL.
    restarted = Database(registry.db.dsn)
    try:
        with operator_server(restarted, registry.clock) as origin:
            connect(page, origin)
            assert inventory(page, origin) == saved
            expect(page.locator("#frames")).to_contain_text(replacement)
            expect(page.locator("#frames")).to_contain_text("Committed r3")
    finally:
        restarted.close()


def test_browser_recovers_after_operator_token_is_rejected(page, installation):
    registry, _, _ = installation
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)

        def revoked_token(route):
            # Send a real invalid credential to the production auth dependency.
            route.continue_(headers={**route.request.headers, "authorization": "Bearer expired"})

        page.route("**/v1/operator/inventory", revoked_token)
        page.get_by_role("button", name="Refresh inventory", exact=True).click()
        expect(page.locator("#message")).to_contain_text("token was not accepted")
        expect(page.locator("#login")).to_be_visible()
        expect(page.locator("#controls")).to_be_hidden()
        page.unroute("**/v1/operator/inventory", revoked_token)
        page.get_by_label("Operator token").fill(ADMIN)
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#controls")).to_be_visible()
        expect(page.locator("#message")).to_have_text("Connected.")


def test_browser_same_token_reconnect_fences_delayed_rejection(page, installation):
    registry, _, _ = installation
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        delayed = []

        def hold_old_content(route):
            if not delayed:
                delayed.append(route)
            else:
                route.continue_()

        def rejected_inventory(route):
            route.continue_(headers={**route.request.headers, "authorization": "Bearer expired"})

        page.route("**/v1/operator/media", hold_old_content)
        with page.expect_request("**/v1/operator/media"):
            page.get_by_role("button", name="Refresh content and health", exact=True).click()
        assert len(delayed) == 1
        page.route("**/v1/operator/inventory", rejected_inventory)
        page.get_by_role("button", name="Refresh inventory", exact=True).click()
        expect(page.locator("#login")).to_be_visible()
        page.unroute("**/v1/operator/inventory", rejected_inventory)

        # Re-enter the SAME credential while the old content request remains pending.
        page.get_by_label("Operator token").fill(ADMIN)
        with page.expect_response(lambda response: response.url.endswith("/v1/operator/media")
                                 and response.status == 200):
            page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#controls")).to_be_visible()
        expect(page.locator("#message")).to_have_text("Connected.")
        with page.expect_response(lambda response: response.url.endswith("/v1/operator/media")
                                 and response.status == 401):
            route = delayed[0]
            route.continue_(headers={**route.request.headers, "authorization": "Bearer expired"})
        page.unroute("**/v1/operator/media", hold_old_content)
        expect(page.get_by_role("button", name="Refresh content and health", exact=True)).to_be_enabled()
        expect(page.locator("#login")).to_be_hidden()
        expect(page.locator("#controls")).to_be_visible()
        page.get_by_role("button", name="Refresh inventory", exact=True).click()
        expect(page.locator("#message")).to_have_text("Inventory refreshed.")


def test_browser_stale_calibration_conflict_refresh_and_preview_expiry(page, context, installation):
    registry, first, _ = installation
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        create_frame(page)
        bind(page, first)
        stale = context.new_page()
        connect(stale, origin)
        path = "/v1/operator/frames/browser-frame/calibration"
        page.locator("#gain").fill("0.8")
        command(page, "Commit", path)
        stale.locator("#gain").fill("0.5")
        command(stale, "Commit", path, status=409)
        expect(stale.locator("#message")).to_have_text(
            "Calibration changed. Refresh and review the saved values."
        )
        assert inventory(page, origin).frames[0].calibration.gain == .8
        stale.get_by_role("button", name="Refresh inventory", exact=True).click()
        expect(stale.locator("#gain")).to_have_value("0.8")
        stale.locator("#gain").fill("0.6")
        command(stale, "Preview", path)
        registry.clock.advance(31)
        stale.get_by_role("button", name="Refresh inventory", exact=True).click()
        expect(stale.locator("#gain")).to_have_value("0.8")
        observed = inventory(page, origin).frames[0]
        assert observed.preview is None and observed.calibration.gain == .8
