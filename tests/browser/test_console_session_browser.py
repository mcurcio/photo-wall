"""Pass A (stay signed in) in a real browser: the sign-in screen, the session cookie and Log out.

The production app on a loopback listener (operator_harness), real Chromium. The page never holds
the token: every operator fetch carries the console marker and the browser's HttpOnly cookie, and
no request carries a bearer. Cookies ignore the port, so two servers on 127.0.0.1 share one
cookie jar: that is how a rotated token and "another address" are exercised for real.
"""

import os
import re

import pytest
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_registry import ADMIN, enroll

from central.operator_session import SESSION_SECONDS
from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "session-1"
ROTATED = "rotated-operator-" + "r" * 40
LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)


def _seed(registry):
    registry.create_frame(FrameCreate(id=FRAME, surface_id="wall", x_mm=100, y_mm=100,
                                      width_mm=400, height_mm=300, profile=LANDSCAPE))


def _frame(page):
    return page.get_by_role("button", name=f"Frame {FRAME}", exact=True)


def _sign_in_button(page):
    return page.get_by_role("button", name="Sign in", exact=True)


def _expect_signed_out(page):
    expect(_sign_in_button(page)).to_be_visible()
    expect(_frame(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Log out", exact=True)).to_have_count(0)


def test_sign_in_once_survives_reload_and_a_second_tab(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame(page)).to_be_visible()
        # The cookie is HttpOnly and nothing is stored: the page cannot read a credential.
        assert page.evaluate("document.cookie") == ""
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0

        page.reload()
        expect(_frame(page)).to_be_visible()
        expect(_sign_in_button(page)).to_have_count(0)

        second = page.context.new_page()
        second.goto(origin + "/console")
        expect(second.get_by_role("button", name=f"Frame {FRAME}", exact=True)).to_be_visible()
        expect(second.get_by_role("button", name="Sign in", exact=True)).to_have_count(0)
        second.close()


def test_log_out_then_reload_stays_signed_out(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame(page)).to_be_visible()
        page.get_by_role("button", name="Log out", exact=True).click()
        _expect_signed_out(page)
        page.reload()
        _expect_signed_out(page)
        expect(page.get_by_role("alert")).to_have_count(0)


def test_session_expiry_after_thirty_days_returns_to_the_sign_in_screen(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame(page)).to_be_visible()
        registry.clock.advance(SESSION_SECONDS)
        page.get_by_role("button", name="Refresh", exact=True).click()
        _expect_signed_out(page)
        expect(page.get_by_role("alert")).to_contain_text(
            "Signed out: the session expired or the token changed")


def test_a_rotated_token_signs_the_browser_out(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as old:
        sign_in(page, old)
        expect(_frame(page)).to_be_visible()
    # A restart with a new token: the browser still sends its cookie (same host), and it fails.
    with operator_server(registry.db, registry.clock, admin_token=ROTATED) as new:
        page.goto(new + "/console")
        _expect_signed_out(page)
        sign_in(page, new, token=ROTATED)
        expect(_frame(page)).to_be_visible()


def test_a_browser_that_drops_the_cookie_is_told_so(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        # Central's 204 arrives without its Set-Cookie, as when cookies are blocked.
        page.route("**/v1/operator/session", lambda route: route.fulfill(status=204))
        sign_in(page, origin)
        expect(page.get_by_role("alert")).to_contain_text(
            "Your browser did not keep the sign-in; allow cookies for this site.")
        _expect_signed_out(page)


def test_the_console_marks_every_operator_fetch_and_never_sends_a_bearer(page, registry):
    _seed(registry)
    sent = []
    page.on("request", lambda request: sent.append(request)
            if "/v1/operator/" in request.url else None)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame(page)).to_be_visible()
        page.get_by_role("button", name="Refresh", exact=True).click()
        page.get_by_role("button", name="Log out", exact=True).click()
        _expect_signed_out(page)
    methods = {request.method for request in sent}
    assert {"GET", "POST", "DELETE"} <= methods
    for request in sent:
        headers = request.all_headers()
        assert "authorization" not in headers, request.url
        assert "x-photo-wall-console" in headers, request.url
    [sign_in_request] = [r for r in sent if r.method == "POST"]
    assert sign_in_request.post_data_json == {"token": ADMIN}


def test_a_write_from_another_address_explains_the_origin_refusal(page, registry):
    identity, _, _ = enroll(registry, count=1)
    player_id = identity["player_id"]
    _seed(registry)
    with operator_server(registry.db, registry.clock) as first, \
            operator_server(registry.db, registry.clock) as second:
        sign_in(page, first)
        expect(_frame(page)).to_be_visible()
        # The same host on another port: the cookie is sent, so reads work...
        page.goto(second + "/console")
        pending = page.get_by_role("group", name="Pending players", exact=True)
        expect(pending.get_by_role("button", name=player_id, exact=True)).to_be_visible()
        # ...but a write is refused for its Origin, and the console says what to do.
        pending.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label(f"Type {player_id[-6:]} to confirm", exact=True).fill(player_id[-6:])
        with page.expect_response(re.compile(r".*/retire$")) as refused:
            dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        assert refused.value.status == 403
        expect(page.get_by_text(
            "Central refused this write because it did not come from the page you signed in on.",
            exact=False)).to_be_visible()
        # Signing in at this address binds it, and the same write then succeeds.
        sign_in(page, second)
        expect(pending.get_by_role("button", name=player_id, exact=True)).to_be_visible()
        pending.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        dialog.get_by_label(f"Type {player_id[-6:]} to confirm", exact=True).fill(player_id[-6:])
        dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        retired = page.get_by_role("group", name="Retired players", exact=True)
        expect(retired.get_by_role("button", name=player_id, exact=True)).to_be_visible()
