"""The week page's Power lane and Night off editor (Power and the week, roadmap 2a; slice S4 K1, K2).

Real Chromium against the production create_app (the operator browser harness): every band is
Central's (GET /v1/operator/week), every press goes through the operator power routes. The house is
Europe/London and the browser runs in Auckland, so a time worked out in the browser would draw the
bands twelve or thirteen hours away. The test clock is Wednesday 14 Oct 2026, 12:00 house time.

Mutation probes: P-lane-local (place band edges from the browser's zone: `houseLocal` with the
browser's zone, or Date parsing of the band's instants) -> the 23:00 bands are drawn elsewhere and
named for other times; P-no-reload (the editor retries a 409 at the current revision) -> the stale
tab's 21:00 overwrites 22:30.
"""

import os

import pytest
from console_tasks import connect
from operator_harness import operator_server
from playwright.sync_api import expect
from power_page import (
    band,
    button,
    hold_banner,
    hold_lasts,
    hold_lasts_choice,
    night_off_band,
    night_off_editor,
    week,
)
from support.console_words import POWER, power
from support.house_time import LONDON, at, set_house

from central.house_time.model import HouseTime, Place
from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NOON = at(LONDON, "2026-10-14T12:00")       # a Wednesday
DAYS = ("Mon 12 Oct", "Tue 13 Oct", "Wed 14 Oct", "Thu 15 Oct", "Fri 16 Oct", "Sat 17 Oct", "Sun 18 Oct")


def seed_house(registry, frame="bedroom"):
    """A London house at Wednesday noon, one Frame, Night off 23:00-07:00 on All Frames."""
    registry.clock.wall = NOON
    set_house(registry.db, LONDON)
    registry.create_frame(FrameCreate(id=frame, width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
    with registry.db.transaction() as conn:   # the seed chose the Frames there were at deploy: none
        conn.execute("""UPDATE night_off SET settings = jsonb_set(settings, '{frames}', '{"kind": "all-frames"}')""")


@pytest.mark.browser_context_args(timezone_id="Pacific/Auckland")
def test_the_lane_draws_night_off_and_holds_with_resume_schedule(page, registry):
    seed_house(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        expect(week(page).get_by_role("heading", name="Week of Mon 12 Oct", exact=True)).to_be_visible()
        # Night off's grey band every night, on the house clock, the on band between.
        # (the week ends at Monday's midnight, so Sunday's night is drawn to it).
        for day, ends in zip(DAYS, (*(f"{day} 07:00" for day in DAYS[1:]), "Mon 19 Oct 00:00")):
            expect(night_off_band(page, day, f"{day} 23:00 – {ends}")).to_have_count(1)
        expect(band(page, "Wed 14 Oct", "^On, Wed 14 Oct 07:00–23:00$")).to_have_count(1)

        # All off: a hatched hold labelled with who set it, until the Frame's next change (23:00).
        held_off = power("heldWho", power="off", who="console")
        button(week(page), "allOff").click()
        expect(band(page, "Wed 14 Oct", f"^{held_off}, Wed 14 Oct 12:00–23:00$")).to_have_count(1)
        holds = hold_banner(page)
        expect(holds.get_by_role("heading", name=power("held", power="off", until="23:00"), exact=True)).to_be_visible()
        expect(week(page)).to_contain_text(power("holding", power="off", until="23:00"))     # Central's until
        expect(holds).to_contain_text(power("holdRow", who=held_off, until="23:00", frames="Frame 1"))

        # Resume schedule removes it at once, and the hold banner with it.
        button(holds, "resume").click()
        expect(band(page, "Wed 14 Oct", "^Held")).to_have_count(0)
        expect(holds).to_have_count(0)
        expect(week(page)).not_to_contain_text(power("held", power="off", until="23:00"))

        # All on, then All off: only the newest hold shows.
        button(week(page), "allOn").click()
        expect(band(page, "Wed 14 Oct", "^" + power("heldWho", power="on", who="console"))).to_have_count(1)
        button(week(page), "allOff").click()
        expect(band(page, "Wed 14 Oct", f"^{held_off}")).to_have_count(1)
        expect(band(page, "Wed 14 Oct", "^Held on")).to_have_count(0)
        expect(week(page)).to_contain_text(power("held", power="off", until="23:00"))


@pytest.mark.browser_context_args(timezone_id="Pacific/Auckland")
def test_each_frames_hold_ends_at_its_own_next_change_and_the_setting_sits_beside_all_off(page, registry):
    """Owner, 2026-10-12: "Each Frame's hold ends at its own next change; a Frame with no Night off
    holds until 07:00 tomorrow; the setting moves beside All off/All on". The bedroom follows Night
    off and the study does not: All off at Wednesday noon holds the bedroom to 23:00 and the study
    to 07:00, one banner row each; "A hold lasts" is chosen beside All off (no Save), and the next
    All on lasts until resumed.

    Mutation probes: (P-house-wide-end) end every Frame's hold at Night off's next on or off time
    -> the study's row reads 23:00; (P-banner-newest-end) title the banner by the newest view's end
    alone -> it reads "Held off until 23:00"."""
    seed_house(registry)
    registry.create_frame(FrameCreate(id="study", width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
    with registry.db.transaction() as conn:   # Night off on the bedroom only
        conn.execute("""UPDATE night_off SET settings = jsonb_set(settings, '{frames}',
                        '{"kind": "frames", "frame_ids": ["bedroom"]}')""")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        expect(hold_lasts_choice(page, "holdLastsNext")).to_be_checked()
        expect(night_off_editor(page).get_by_role("radiogroup", name=POWER["holdLasts"])).to_have_count(0)

        held_off = power("heldWho", power="off", who="console")
        button(week(page), "allOff").click()
        holds = hold_banner(page)
        expect(holds.get_by_role("heading", name=power("heldEach", power="off"), exact=True)).to_be_visible()
        expect(week(page)).to_contain_text(power("holdingEach", power="off"))
        expect(holds).to_contain_text(power("holdRow", who=held_off, until="23:00", frames="Frame 1"))
        expect(holds).to_contain_text(power("holdRow", who=held_off, until="07:00", frames="Frame 2"))
        expect(band(page, "Wed 14 Oct", f"^{held_off}, Wed 14 Oct 12:00 – Thu 15 Oct 07:00$")).to_have_count(1)

        with page.expect_response(lambda r: r.url.endswith("/power/hold-lasts") and r.request.method == "PUT") as sent:
            hold_lasts_choice(page, "holdLastsResumed").click()
        assert sent.value.request.post_data_json == {"hold_lasts": "until-resumed"}
        expect(week(page)).to_contain_text(power("holdLastsSaved", setting=POWER["holdLastsResumed"]))
        expect(holds).to_contain_text(power("holdRow", who=held_off, until="07:00", frames="Frame 2"))  # kept
        button(week(page), "allOn").click()
        expect(week(page)).to_contain_text(power("holding", power="on", until=POWER["youResume"]))
        expect(hold_lasts(page)).to_be_visible()


@pytest.mark.browser_context_args(timezone_id="Pacific/Auckland")
def test_night_off_saves_moves_the_lane_and_a_stale_tab_reloads(page, registry):
    seed_house(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        editor = night_off_editor(page)
        # No Place: sunrise and sunset wait, with the way to set the house location.
        expect(editor.get_by_role("radiogroup", name="Turn off at", exact=True).get_by_role(
            "radio", name="Sunset", exact=True)).to_be_disabled()
        expect(editor.get_by_role("link", name="Set the house location").first).to_have_attribute("href", "#/house")

        second = page.context.new_page()
        second.goto(origin + "/console#/schedule")
        stale = night_off_editor(second)
        expect(stale.get_by_label("Turn off", exact=True)).to_have_value("23:00")

        editor.get_by_label("Turn off", exact=True).fill("22:30")
        button(editor, "saveNightOff").click()
        expect(editor.get_by_role("status")).to_have_text("Saved. The displays follow it from its next off or on.")
        expect(night_off_band(page, "Wed 14 Oct", "Wed 14 Oct 22:30 – Thu 15 Oct 07:00")).to_have_count(1)

        # The other tab opened Night off before that save: its Save is refused, never overwrites.
        stale.get_by_label("Turn off", exact=True).fill("21:00")
        with second.expect_response(lambda r: r.url.endswith("/power/night-off") and r.request.method == "PUT") as sent:
            button(stale, "saveNightOff").click()
        assert sent.value.status == 409
        expect(stale).to_contain_text(POWER["nightOffChanged"])
        expect(button(stale, "saveNightOff")).to_be_disabled()
        button(stale, "reloadNightOff").click()
        expect(stale.get_by_label("Turn off", exact=True)).to_have_value("22:30")
        assert page.request.get(origin + "/v1/operator/power/night-off").json()["night_off"]["off"] == {
            "at": "clock", "time": "22:30"}

        # With a location, "30 minutes before sunset" is chosen as a side and a number of minutes:
        # a typed minus sign was lost on the way (it saved 30 after).
        set_house(registry.db, HouseTime(zone=LONDON.zone, place=Place(latitude=51.5, longitude=-0.13)))
        page.reload()
        editor = night_off_editor(page)
        editor.get_by_role("radiogroup", name="Turn off at", exact=True).get_by_role(
            "radio", name="Sunset", exact=True).click()
        editor.get_by_role("radiogroup", name="Turn off: before or after sunset", exact=True).get_by_role(
            "radio", name="Before", exact=True).click()
        editor.get_by_label("Turn off: minutes before sunset", exact=True).fill("30")
        button(editor, "saveNightOff").click()
        expect(editor.get_by_role("status")).to_have_text("Saved. The displays follow it from its next off or on.")
        assert page.request.get(origin + "/v1/operator/power/night-off").json()["night_off"]["off"] == {
            "at": "sunset", "offset_minutes": -30}


PHONE = {"width": 390, "height": 844}


def _small_targets(scope):
    """The visible controls in `scope` under 44 px tall or wide (design language §2)."""
    return scope.evaluate("""root => [...root.querySelectorAll(
        'button, input, select, [role=radio], [role=checkbox], [role=switch]')]
        .filter(el => el.offsetParent !== null)
        .map(el => [el.getAttribute('aria-label') || el.textContent.trim() || el.tagName, el.getBoundingClientRect()])
        .filter(([, box]) => box.height < 44 || box.width < 44)
        .map(([name, box]) => `${name}: ${Math.round(box.width)} x ${Math.round(box.height)}`)""")


@pytest.mark.browser_context_args(timezone_id="Pacific/Auckland", viewport=PHONE)
def test_at_phone_width_the_week_is_a_day_at_a_time_with_no_sideways_scroll_and_44px_targets(page, registry):
    seed_house(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        days = week(page).get_by_role("group", name="Week of Mon 12 Oct: choose a day", exact=True)
        # Today (the house's Wednesday) is shown first; only its bands are drawn.
        expect(days.get_by_role("button", name="Wed 14 Oct", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(night_off_band(page, "Wed 14 Oct", "Wed 14 Oct 23:00 – Thu 15 Oct 07:00")).to_have_count(1)
        expect(week(page).get_by_role("group", name="Thu 15 Oct", exact=True)).to_have_count(0)
        days.get_by_role("button", name="Thu 15 Oct", exact=True).click()
        expect(night_off_band(page, "Thu 15 Oct", "Thu 15 Oct 23:00 – Fri 16 Oct 07:00")).to_have_count(1)
        expect(week(page).get_by_role("group", name="Wed 14 Oct", exact=True)).to_have_count(0)
        # The colour key says what each band means.
        expect(week(page).get_by_role("list", name="Colour key", exact=True).get_by_role("listitem")).to_have_count(4)

        button(week(page), "allOff").click()
        expect(week(page).get_by_role("region", name="Holds", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the page scrolls sideways"
        small = _small_targets(week(page).element_handle())
        assert small == [], small
