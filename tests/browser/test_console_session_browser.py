"""Pass A (stay signed in) in a real browser: the sign-in screen, the session cookie and Log out.

The production app on a loopback listener (operator_harness), real Chromium. The page never holds
the token: every operator fetch carries the console marker and the browser's HttpOnly cookie, and
no request carries a bearer. Cookies ignore the port, so two servers on 127.0.0.1 share one
cookie jar: that is how a rotated token and "another address" are exercised for real.

Signed in, the console lands on Home (decision 0036), whose tile for the Frame is the proof that
Central's reads rendered. A session that ends while signed in shows the sign-in screen
as an overlay over the kept, hidden console (flow design §6 (a)).

Provider mode (decision 0035) runs the same app with an identity provider configured
(operator_harness.provider_server: a FakeProvider served over HTTP, which Central's real OpenID
Connect client and the browser both reach). Central and the provider read one controlled clock.
"""

import os
import re
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from console_tasks import current_hash, go, open_pi, section_heading
from operator_harness import (
    PERSON,
    PERSON_SUBJECT,
    PROVIDER,
    answer_first,
    assert_fits_width,
    operator_server,
    provider_server,
    provider_sign_in_button,
    sign_in,
    sign_in_with_provider,
    submit_sign_in,
)
from playwright.sync_api import expect
from support.frames import frame_create
from test_registry import ADMIN, enroll

from central.operator_access.model import ProviderRefused
from central.operator_session import SESSION_SECONDS, SessionCodec

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "session-1"
ROTATED = "rotated-operator-" + "r" * 40


def _seed(registry):
    registry.create_frame(frame_create(FRAME, x_mm=100, y_mm=100), by="console")


def _frame(page):
    """The Frame's tile on Home (Central names a new Frame "Frame 1"): visible only while signed in
    and shown."""
    return page.get_by_role("region", name="Wall 1", exact=True).get_by_role("group", name="Frame 1", exact=True)


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
        expect(_frame(second)).to_be_visible()
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
        page.evaluate("window.__notReloaded = true")
        registry.clock.advance(SESSION_SECONDS)
        # Home has no Refresh button: its next read (every 5 s) finds the session ended.
        expect(_sign_in_button(page)).to_be_visible(timeout=15000)
        _expect_signed_out(page)
        expect(page.get_by_role("alert")).to_contain_text(
            "Signed out: the session expired or the token changed")
        # The sign-in screen overlays the console, which keeps its last snapshot, hidden
        # and inert (flow design §6 (a)), and says the work is kept.
        expect(page.get_by_text("Your unsaved work is kept until you sign in again.")
               ).to_be_visible()
        kept = page.get_by_role("link", name="Frame 1", exact=True, include_hidden=True)   # Home's tile
        expect(kept).to_be_attached()
        expect(kept).to_be_hidden()
        # Signing in again shows the same console, without a reload.
        submit_sign_in(page)
        expect(_frame(page)).to_be_visible()
        expect(_sign_in_button(page)).to_have_count(0)
        assert page.evaluate("window.__notReloaded") is True


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
    with operator_server(registry.db, registry.clock) as origin:
        page.context.clear_cookies()
        page.goto(origin + "/console")
        expect(_sign_in_button(page)).to_be_visible()
        failed = answer_first(page, "**/v1/operator/snapshot",
                              lambda route: route.fulfill(status=500))
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
        page.evaluate("fetch('/healthz').then((response) => response.status)")  # answered
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
        go(page, "wall")    # Home has no Refresh button
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
    identity, _, request = enroll(registry, count=1)
    player_id = identity["player_id"]
    name = f"Player {request.device_id}"
    _seed(registry)
    with operator_server(registry.db, registry.clock) as first, \
            operator_server(registry.db, registry.clock) as second:
        sign_in(page, first)
        expect(_frame(page)).to_be_visible()
        # The same host on another port: the cookie is sent, so reads work...
        page.goto(second + f"/console#/hardware/{request.device_id}")
        player = page.locator("main > section:not([hidden])")
        expect(page.get_by_role("heading", level=2, name=name, exact=True)).to_be_visible()
        # ...but a write is refused for its Origin, and the console says what to do.
        player.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
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
        open_pi(page, name)
        player.get_by_role("button", name=f"Retire player {player_id}", exact=True).click()
        dialog.get_by_label(f"Type {player_id[-6:]} to confirm", exact=True).fill(player_id[-6:])
        dialog.get_by_role("button", name="Confirm retire", exact=True).click()
        expect(player).to_contain_text("Standing: Retired")


# --- Provider mode (decision 0035): sign-in through the deployment's identity provider.

SESSION = "/v1/operator/session"
NARROW = {"width": 390, "height": 844}
# The access token's life (FakeProvider `access_seconds`): how long a session's judgement is fresh.
JUDGEMENT_SECONDS = 300


def _dialog(page):
    return page.get_by_role("dialog", name="Sign in to Photo Wall", exact=True)


def _expect_not_signed_in(page, words):
    """The sign-in screen with its one notice, in the card: the chip, then why."""
    expect(provider_sign_in_button(page)).to_be_visible()
    alert = _dialog(page).get_by_role("alert")
    expect(alert).to_have_count(1)
    expect(alert).to_contain_text("Not signed in")
    expect(alert).to_contain_text(words)
    expect(_frame(page)).to_have_count(0)


def _people(page):
    return page.get_by_role("region", name="People and access", exact=True)


def test_first_load_in_provider_mode_signs_in_through_the_provider_and_lands_on_home(
        page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, fake):
        page.context.clear_cookies()
        page.goto(origin + "/console")
        expect(provider_sign_in_button(page)).to_be_visible()
        # Provider mode offers no token field, and names no token.
        expect(page.get_by_label("Operator token")).to_have_count(0)
        expect(_dialog(page)).not_to_contain_text("token")
        provider_sign_in_button(page).click()
        expect(_frame(page)).to_be_visible()
        expect(section_heading(page, "home")).to_be_visible()
        expect(_dialog(page)).to_be_hidden()
        expect(page.get_by_role("alert")).to_have_count(0)
        # One code flow, asked of the provider by Central; the page holds no credential.
        assert [request["grant_type"] for request in fake.token_requests] == ["authorization_code"]
        assert page.evaluate("document.cookie") == ""
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        [cookie] = page.context.cookies()
        assert (cookie["name"], cookie["path"], cookie["httpOnly"], cookie["sameSite"]) == (
            "photo_wall_session", "/v1/operator/", True, "Strict")
        page.reload()
        expect(_frame(page)).to_be_visible()
        expect(_dialog(page)).to_be_hidden()


def test_a_deep_link_is_returned_to_after_signing_in_through_the_provider(page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, _fake):
        sign_in_with_provider(page, origin, "#/scenes")
        expect(section_heading(page, "scenes")).to_be_visible()
        assert current_hash(page) == "#/scenes"
        assert urlsplit(page.url).query == ""


def test_a_person_without_the_role_is_told_they_are_not_allowed(page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock, roles=()) as (origin, _fake):
        sign_in_with_provider(page, origin)
        _expect_not_signed_in(
            page, f"Your account is not allowed to use Photo Wall. Ask for the owner role in {PROVIDER}.")
        # The result is read once and taken out of the address: a reload does not repeat it.
        assert "sign_in" not in page.url
        assert page.context.cookies(origin + SESSION) == []
        assert page.request.get(origin + SESSION).status == 401
        page.reload()
        expect(provider_sign_in_button(page)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)


def test_a_provider_that_cannot_be_reached_says_so_on_the_sign_in_screen(page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, fake):
        page.context.clear_cookies()
        page.goto(origin + "/console")
        expect(provider_sign_in_button(page)).to_be_visible()
        fake.unreachable = True
        provider_sign_in_button(page).click()
        _expect_not_signed_in(page, f"Can't reach {PROVIDER}. Photo Wall keeps playing. Try again.")
        assert "sign_in" not in page.url
        # Trying again once the provider answers signs in.
        fake.unreachable = False
        provider_sign_in_button(page).click()
        expect(_frame(page)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)


def test_a_session_ending_in_provider_mode_signs_in_again_in_a_new_tab_and_keeps_the_draft(
        page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, fake):
        sign_in_with_provider(page, origin)
        expect(_frame(page)).to_be_visible()
        go(page, "house")
        # By its label alone: get_by_label, unlike get_by_role, still finds it under the overlay.
        latitude = page.get_by_label("Latitude", exact=True)
        latitude.fill("40.7")  # an unsaved draft
        page.evaluate("window.__notReloaded = true")

        # The provider's own session ends; Central learns it when the judgement goes stale.
        with registry.db.transaction() as conn:
            [row] = conn.execute("SELECT provider_session FROM operator_sessions").fetchall()
        fake.end_session(row["provider_session"])
        registry.clock.advance(JUDGEMENT_SECONDS)
        page.get_by_role("button", name="Refresh", exact=True).click()

        expect(provider_sign_in_button(page)).to_be_visible()
        alert = _dialog(page).get_by_role("alert")
        expect(alert).to_contain_text("Not signed in")
        expect(alert).to_contain_text("Signed out: your sign-in ended. Sign in again.")
        expect(_dialog(page).get_by_text("Your unsaved work is kept until you sign in again.")
               ).to_be_visible()
        # The console is kept underneath, hidden and inert, with the draft.
        assert page.evaluate("document.querySelector('.shell').inert") is True
        expect(latitude).to_be_hidden()
        assert latitude.evaluate("(input) => input.value") == "40.7"
        assert page.request.get(origin + SESSION).status == 401

        # Signing in again happens in a new tab, which lands signed in on the same route.
        with page.context.expect_page() as opened:
            provider_sign_in_button(page).click()
        tab = opened.value
        expect(section_heading(tab, "house")).to_be_visible()
        assert urlsplit(tab.url).path == "/" and urlsplit(tab.url).fragment == "/house"

        # This tab notices within its 2 s check and resumes where it was, without a reload.
        expect(_dialog(page)).to_be_hidden()
        expect(section_heading(page, "house")).to_be_visible()
        expect(latitude).to_be_visible()
        expect(latitude).to_have_value("40.7")
        expect(page.get_by_role("alert")).to_have_count(0)
        assert page.evaluate("window.__notReloaded") is True
        assert current_hash(page) == "#/house"
        tab.close()


def test_log_out_in_provider_mode_ends_the_session_at_central_and_at_the_provider(page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, fake):
        visits = []
        page.on("request", lambda request: visits.append(request.url)
                if request.url.startswith(fake.issuer + "/logout") else None)
        sign_in_with_provider(page, origin)
        expect(_frame(page)).to_be_visible()
        [cookie] = page.context.cookies()
        held = {"Cookie": f"{cookie['name']}={cookie['value']}"}
        assert page.request.get(origin + SESSION, headers=held).status == 200

        page.get_by_role("banner").get_by_role("button", name="Log out", exact=True).click()
        # The provider's end-session address comes back to the console's sign-in page.
        expect(provider_sign_in_button(page)).to_be_visible()
        expect(_frame(page)).to_have_count(0)
        expect(page.get_by_role("alert")).to_have_count(0)
        assert urlsplit(page.url)._replace(fragment="").geturl() == origin + "/"

        # The cookie is gone, and the value it held authenticates nothing any more.
        assert page.context.cookies() == []
        assert page.request.get(origin + SESSION).status == 401
        assert page.request.get(origin + SESSION, headers=held).status == 401
        with registry.db.transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM operator_sessions").fetchone()["n"] == 0

        # The browser went to the provider's end-session address with the person's ID token.
        [visit] = visits
        query = parse_qs(urlsplit(visit).query)
        assert query["post_logout_redirect_uri"] == [origin + "/"]
        [hint] = query["id_token_hint"]
        claims = fake.signature(hint, "RS256", jwt.get_unverified_header(hint)["kid"])
        assert (claims["sub"], claims["typ"]) == (PERSON_SUBJECT, "ID")
        # So the provider's session ended too: its refresh token is refused.
        [issued] = fake.issued
        with pytest.raises(ProviderRefused, match="invalid_grant"):
            fake.refresh(issued["refresh_token"])


def test_people_and_access_in_provider_mode_names_the_person_the_provider_and_the_limit(
        page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, _fake):
        sign_in_with_provider(page, origin)
        expect(_frame(page)).to_be_visible()
        go(page, "people-and-access")
        assert current_hash(page) == "#/people-and-access"
        people = _people(page)
        expect(people.get_by_label("Signed in as", exact=True)).to_have_text(f"{PERSON} · Owner")
        expect(people.get_by_role("button", name="Log out", exact=True)).to_be_visible()
        expect(people.get_by_label("Accounts", exact=True)).to_have_text(
            f"Set by your deployment: {PROVIDER}")
        expect(people).to_contain_text("Connected")
        expect(people).not_to_contain_text("Can't tell")
        expect(people.get_by_label("Sign-in lasts", exact=True)).to_have_text(
            f"As {PROVIDER} decides, at most 30 days")
        expect(page.get_by_text("No accounts:")).to_have_count(0)


def test_people_and_access_says_cant_tell_while_central_cannot_reach_the_provider(page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, fake):
        sign_in_with_provider(page, origin)
        expect(_frame(page)).to_be_visible()
        fake.unreachable = True
        go(page, "people-and-access")
        people = _people(page)
        # The person stays signed in; the check is never a guess that the provider is down.
        expect(people.get_by_label("Signed in as", exact=True)).to_have_text(f"{PERSON} · Owner")
        expect(people).to_contain_text("Can't tell")
        expect(people).not_to_contain_text("Connected")
        expect(people).to_contain_text(f"Nobody new can sign in until Central reaches {PROVIDER}.")


def test_log_out_on_people_and_access_logs_out(page, registry):
    _seed(registry)
    with provider_server(registry.db, registry.clock) as (origin, _fake):
        sign_in_with_provider(page, origin)
        expect(_frame(page)).to_be_visible()
        go(page, "people-and-access")
        _people(page).get_by_role("button", name="Log out", exact=True).click()
        expect(provider_sign_in_button(page)).to_be_visible()
        assert page.request.get(origin + SESSION).status == 401


def test_people_and_access_in_token_mode_says_there_are_no_accounts(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        expect(_frame(page)).to_be_visible()
        go(page, "people-and-access")
        expect(page.get_by_text(
            "No accounts: this Photo Wall uses one shared token."
            " Your deployment can connect an identity provider.", exact=True)).to_be_visible()
        expect(page.get_by_text("Signed in as")).to_have_count(0)
        expect(page.get_by_text("Sign-in lasts")).to_have_count(0)


def test_provider_sign_in_and_people_and_access_never_scroll_sideways_on_a_phone(page, registry):
    _seed(registry)
    page.set_viewport_size(NARROW)
    with provider_server(registry.db, registry.clock, roles=()) as (origin, fake):
        # The sign-in screen, plain and with its longest notice.
        page.context.clear_cookies()
        page.goto(origin + "/console")
        button = provider_sign_in_button(page)
        expect(button).to_be_visible()
        assert_fits_width(page, "provider sign-in")
        # Phone: the button is full width (of the card) and at least 44 px high.
        box, card = button.bounding_box(), page.locator(".signin__card").bounding_box()
        assert box["height"] >= 44, box
        assert box["width"] >= card["width"] * .8, (box, card)
        button.click()
        expect(_dialog(page).get_by_role("alert")).to_contain_text("is not allowed to use Photo Wall")
        assert_fits_width(page, "provider sign-in, not allowed")

        fake.grant_role(PERSON_SUBJECT)
        provider_sign_in_button(page).click()
        expect(_frame(page)).to_be_visible()
        go(page, "people-and-access")
        expect(_people(page).get_by_label("Sign-in lasts", exact=True)).to_be_visible()
        assert_fits_width(page, "People and access")
