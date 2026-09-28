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

from central.operator_session import SESSION_SECONDS, SessionCodec
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


def test_a_sign_in_whose_first_read_fails_keeps_checking_until_a_poll_recovers(page, registry):
    # SigningIn -> Checking on 204: a 500 on the first read must not strand the tab on the form.
    _seed(registry)
    failed = []
    with operator_server(registry.db, registry.clock) as origin:
        page.context.clear_cookies()
        page.goto(origin + "/console")
        expect(_sign_in_button(page)).to_be_visible()
        page.route("**/v1/operator/inventory",
                   lambda route: (failed.append(route.request.url), route.fulfill(status=500)),
                   times=1)
        page.get_by_label("Operator token").fill(ADMIN)
        _sign_in_button(page).click()
        expect(_sign_in_button(page)).to_have_count(0)
        assert len(failed) == 1
        expect(_frame(page)).to_be_visible(timeout=15000)  # the next 5 s poll
        expect(page.get_by_role("alert")).to_have_count(0)


def test_the_session_cookie_is_scoped_to_the_operator_api(page, registry):
    # Cookies ignore the port: a Path=/ cookie would reach every server on this host.
    _seed(registry)
    sent = []
    page.on("request", lambda request: sent.append(
        (request.url, request.all_headers().get("cookie"))))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame(page)).to_be_visible()
        [cookie] = page.context.cookies()
        assert (cookie["name"], cookie["path"], cookie["httpOnly"], cookie["sameSite"]) == (
            "photo_wall_session", "/v1/operator/", True, "Strict")
        sent.clear()
        page.reload()
        expect(_frame(page)).to_be_visible()
        page.evaluate("fetch('/healthz')")
        page.wait_for_timeout(200)
    operator = [cookie for url, cookie in sent if "/v1/operator/" in url]
    others = [(url, cookie) for url, cookie in sent if "/v1/operator/" not in url]
    assert operator and all(cookie for cookie in operator)
    assert any(url.endswith("/console") for url, _ in others)
    assert any(url.endswith("/healthz") for url, _ in others)
    assert all(cookie is None for _, cookie in others), others


def _legacy_cookie(registry, origin):
    """A pre-scoping session cookie, as a browser signed in before the upgrade holds it."""
    value = SessionCodec(ADMIN, registry.clock).mint(origin)
    return {"name": "photo_wall_session", "value": value, "url": origin + "/",
            "httpOnly": True, "sameSite": "Strict"}


def test_a_legacy_path_root_cookie_stays_signed_in_and_sign_in_or_log_out_clears_it(
        page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        page.context.clear_cookies()
        page.context.add_cookies([_legacy_cookie(registry, origin)])
        assert [c["path"] for c in page.context.cookies()] == ["/"]
        page.goto(origin + "/console")
        expect(_frame(page)).to_be_visible()  # accepted until it expires
        # Signing in again replaces it with the scoped cookie.
        response = page.request.post(origin + "/v1/operator/session", data={"token": ADMIN},
                                     headers={"X-Photo-Wall-Console": "1", "Origin": origin})
        assert response.status == 204
        assert [(c["name"], c["path"]) for c in page.context.cookies()] == [
            ("photo_wall_session", "/v1/operator/")]
        # Log out clears a legacy cookie too.
        page.context.add_cookies([_legacy_cookie(registry, origin)])
        assert len(page.context.cookies()) == 2
        page.reload()
        page.get_by_role("button", name="Log out", exact=True).click()
        _expect_signed_out(page)
        assert page.context.cookies() == []
        page.reload()
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
