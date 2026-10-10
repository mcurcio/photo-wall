"""The Frame page's Power tab: the power method and its two switches, and Test off and on (roadmap
1b, slice K2; design-language §8 row B6; console design §5 Power; owner q1, 2026-10-10: "Test: turn
off" lasts 5 minutes, or less if Turn on is pressed).

Every write goes through the operator API the tab calls; the document change that carries a test is
recorded by the worker's own document source (`test_display_power_requests._projected`), and every
Output report travels the link store's commit and its judge (`_answer`), so the tab's words come from
Central's real status, never from the console alone.

Mutation probes: show the result before the report names the change -> the waiting case fails; drop
the power tab from routes.js `TABS` -> tests/test_console_routes_r4.py's pin (and the first case here)
fails.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from console_tasks import connect, current_hash, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_display_identity import NO_SERIAL, _frame, enroll_pi
from test_display_identity import _report as _recorded
from test_display_power_requests import SERIAL, _answer, _bound_frame, _projected, _rows

from contracts.node_output import InForce, PowerMethod, PowerResult

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

WAITING = "Waiting for the Pi"


def _off_thread(call, *args, **kwargs):
    """A recording helper off Playwright's thread, whose event loop is running."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call, *args, **kwargs).result()


def _change(registry) -> int:
    """The worker's source projects (and records) HDMI-A-1's document now; its change."""
    return _off_thread(_projected, registry).change


def _reported(registry, for_change: int, result: PowerResult, in_force: InForce | None) -> None:
    _off_thread(_answer, registry, for_change, result, in_force)


def _seen_frame(registry) -> str:
    """A bound Frame whose Pi reported a no-serial display and answered the standing document."""
    frame, _ = _bound_frame(registry)
    _off_thread(_recorded, registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    _reported(registry, _change(registry), PowerResult.CONFIRMED, InForce("standing", None))
    return frame


def _power(page):
    return page.get_by_role("region", name="Power", exact=True)


def _tests(page) -> list:
    """Every Test request the page sends, as its JSON body."""
    sent = []
    page.on("request", lambda request: sent.append(request.post_data_json)
            if request.url.endswith("/power-tests") and request.method == "POST" else None)
    return sent


def test_test_off_waits_for_the_pi_then_reads_confirmed_off_and_turn_on_replaces_it(page, registry):
    frame = _seen_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        sent = _tests(page)
        power = _power(open_frame(page, frame, "power"))
        assert current_hash(page).endswith(f"/frames/{frame}/power")

        power.get_by_role("button", name="Test: turn off", exact=True).click()
        status = power.get_by_role("status")
        expect(status).to_have_text(WAITING)
        assert sent == [{"power": "off"}]

        test_id = _rows(registry, "SELECT id FROM power_requests")[0]["id"]
        carrying = _change(registry)                    # the document that carries the test
        page.wait_for_timeout(2500)                     # a poll later: the Pi still names the older change
        expect(status).to_have_text(WAITING)
        expect(power).not_to_contain_text("Display confirmed")

        _reported(registry, carrying, PowerResult.CONFIRMED, InForce(str(test_id), 299))
        expect(status).to_have_text("Display confirmed off")
        assert sent == [{"power": "off"}]               # posted once

        power.get_by_role("button", name="Turn on", exact=True).click()
        expect(status).to_have_text(WAITING)
        replacing = _rows(registry, "SELECT id, power FROM power_requests")
        assert [row["power"] for row in replacing] == ["on"]       # one row: Turn on replaced the test
        _reported(registry, _change(registry), PowerResult.CONFIRMED, InForce(str(replacing[0]["id"]), 300))
        expect(status).to_have_text("Display confirmed on")
        assert sent == [{"power": "off"}, {"power": "on"}]


def test_an_unanswered_test_reads_display_did_not_answer(page, registry):
    frame = _seen_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        power = _power(open_frame(page, frame, "power"))
        power.get_by_role("button", name="Test: turn off", exact=True).click()
        expect(power.get_by_role("status")).to_have_text(WAITING)
        test_id = _rows(registry, "SELECT id FROM power_requests")[0]["id"]
        _reported(registry, _change(registry), PowerResult.DID_NOT_ANSWER, InForce(str(test_id), 299))
        expect(power.get_by_role("status")).to_have_text("Display didn't answer")


def test_a_method_change_saves_by_itself(page, registry):
    frame = _seen_frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        power = _power(open_frame(page, frame, "power"))
        method = power.get_by_role("combobox", name="Power method", exact=True)
        expect(method).to_have_value("best-detected")
        with page.expect_request(lambda request: "/power-settings" in request.url
                                 and request.method == "PUT") as captured:
            method.select_option("ddc-ci")
        assert captured.value.post_data_json == {
            "power_method": "ddc-ci", "switch_input_on_power_on": True, "never_off_on_other_input": True}
        expect(power.locator('[data-setting="Power method"]')).to_contain_text("Saved")
        assert _off_thread(_projected, registry).method is PowerMethod.DDC_CI

        open_frame(page, frame, "overview")
        power = _power(open_frame(page, frame, "power"))
        expect(power.get_by_role("combobox", name="Power method", exact=True)).to_have_value("ddc-ci")


def test_an_unbound_frame_asks_which_pi_and_hdmi_port_feeds_it(page, registry):
    enroll_pi(registry, SERIAL)
    frame = _frame(registry, "hallway")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        power = _power(open_frame(page, frame, "power"))
        expect(power).to_contain_text("Choose which Pi and HDMI port feeds this Frame")
        expect(power.get_by_role("button", name="Test: turn off", exact=True)).to_have_count(0)
        power.get_by_role("link", name="Choose on the Hardware tab", exact=True).click()
        assert current_hash(page).endswith(f"/frames/{frame}/hardware")
