"""Central's error in the console: Needs attention's source row (the room lights Central is in doubt
of, with what to do and the Frames that use them), House › Lights' "Can't read since …" and "Link
closed since …" lines, and the Frame page's "Central's error" on Overview, apart from Hardware.

The production app serves the console and the Frame (a real Registry); Central's error routes
(GET /v1/operator/central-errors, GET /v1/operator/frames/{id}/central-error) and the house's lights
with their doubts (GET /v1/operator/house/lights) are the central lanes', so `_serve` answers them
with the contract's shapes (central/central_errors/views.py, central/room_light/views.py).

Mutation probes: drop `noneInDoubt` from Needs attention's "Nothing needs attention" -> the failed
read case reads "Nothing needs attention"; `keptWords` says "Can't tell" for every reason -> the
House › Lights case never reads "Can't read since"; the Frame page shows Central's error on the
Hardware tab instead of Overview -> the Frame case finds no "Central's error" section.
"""

import json
import os
import re

import pytest
from console_tasks import connect, open_frame, visible_page
from operator_harness import operator_server
from playwright.sync_api import expect
from test_console_frame_page_browser import FRAME, _seed
from test_house_lights_browser import _states

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

# Central's clock: the doubts' "since".
SINCE = 1_791_000_000
HOME_ASSISTANT = "home-assistant"
FAMILY_ROOM_REF = {"source": HOME_ASSISTANT, "address": "light.family_room"}
WINDOW_REF = {"source": HOME_ASSISTANT, "address": "sensor.window_illuminance"}
UPDATE_GALLERIA = "Update Galleria in Home Assistant (HACS) to its newest release"
LINE1 = "Photo Wall can't read Family Room Lights · since 29 Oct 18:05"
LINE2 = "Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall"


# Family Room Lights' colour line, following its bulb (its doubt added by `lights_view`).
BULB_LINE = {"colour": {"kind": "kelvin", "kelvin": 2700}, "source": "its-bulb", "sensor": None,
             "colour_from": "following", "stated_at": SINCE - 86400}


def lights_view(*, doubts=True, shape="doubt"):
    """House › Lights' RoomLightsView: Family Room Lights (reports itself, last heard off) and the
    daylight a smart Sensor measures. With `doubts`, Central can't read Family Room Lights and its link
    to the Sensor closed. `shape="cant_tell_since"` is the view before this delivery (no doubts)."""
    cant_read = {"reason": "cant-read", "since": SINCE} if doubts else None
    link_closed = {"reason": "link-closed", "since": SINCE - 600} if doubts else None

    def doubted(record, doubt):
        if shape == "doubt":
            return {**record, "doubt": doubt}
        return {**record, "cant_tell_since": None}

    family = doubted({
        "id": "family-room-lights",
        "light": {"kind": "reported", "name": "Family Room Lights", "reporter": FAMILY_ROOM_REF,
                  "colour": {"kind": "its-bulb", "typed_kelvin": 2700}},
        "entry": {"kind": "reported", "colour": {"kind": "kelvin", "kelvin": 2700},
                  "state": {"kind": "dimmer", "on": False, "brightness": 0}, "doubt": None},
        "heard_at": SINCE - 86400, "colour": doubted(BULB_LINE, cant_read), "named_by": [FRAME],
    }, cant_read)
    daylight = doubted({
        "id": "daylight",
        "light": {"kind": "measured", "name": "Daylight", "sensor": "window-meter", "colour": {"kind": "typed", "kelvin": 6500}},
        "entry": {"kind": "measured", "colour": {"kind": "kelvin", "kelvin": 6500}, "state": {"kind": "measured", "lux": 0},
                  "doubt": None},
        "heard_at": SINCE - 700,
        "colour": doubted({"colour": {"kind": "kelvin", "kelvin": 6500}, "source": "typed", "sensor": None,
                           "colour_from": "typed", "stated_at": None}, None),
        "named_by": [FRAME],
    }, None)
    meter = doubted({"id": "window-meter", "sensor": {"name": "Window meter", "at": WINDOW_REF},
                     "reading": {"kind": "measured", "lux": 0}, "heard_at": SINCE - 700, "reports": "daylight"},
                    link_closed)
    return {"lights": [family, daylight], "sensors": [meter], "colour_sensors": []}


IN_DOUBT = {"in_doubt": [{
    **FAMILY_ROOM_REF, "doubt": {"reason": "cant-read", "since": SINCE}, "lights": ["family-room-lights"],
    "frames": [FRAME], "what_to_do": UPDATE_GALLERIA,
}]}
IMPEDED = {"frame_id": FRAME, "impediment": {"kind": "impeded", "by": {
    "frame_id": FRAME, "output_id": "HDMI-A-1", "grant_id": "3f0c2a52-6a1e-4c0b-9a51-2d3c1f6b7e10",
    "lights": ["family-room-lights"], "words": {"line1": LINE1, "line2": LINE2}}}}


def _json(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _serve(page, *, errors=IN_DOUBT, errors_status=200, lights=None, impediment=IMPEDED):
    """Answer the central lanes' routes with the contract's shapes: Central's errors, the house's lights
    (GET only; writes pass through) and the Frame's impediment."""
    lights = lights_view() if lights is None else lights
    page.route("**/v1/operator/central-errors", lambda route: _json(route, errors, errors_status))
    page.route("**/v1/operator/house/lights", lambda route: _json(route, lights) if route.request.method == "GET"
               else route.fallback())
    page.route(f"**/v1/operator/frames/{FRAME}/central-error", lambda route: _json(route, impediment))
    _states(page)


def _room_lights(page):
    return visible_page(page).get_by_role("region", name="Room lights", exact=True)


def test_needs_attention_says_the_source_in_doubt_what_to_do_and_its_frames(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _serve(page)
        connect(page, origin, "attention")
        section = _room_lights(page)
        expect(section).to_contain_text(re.compile(r"Family Room Lights: Can't read since \S+ \d"))
        expect(section).to_contain_text(f"Photo Wall can't read what its source reports. {UPDATE_GALLERIA}.")
        expect(section).to_contain_text("Until then, the Frames that use it draw with its last word.")
        expect(section.get_by_role("link", name="Open House › Lights", exact=True)).to_have_attribute("href", "#/lights")
        # The Frame by its name (the Registry's "Frame 1"), to its page.
        expect(section.get_by_role("link", name="Frame 1", exact=True)).to_have_attribute(
            "href", re.compile(rf"^#/wall/frames/{FRAME}/"))
        expect(visible_page(page)).not_to_contain_text("Nothing needs attention")


def test_needs_attention_never_says_nothing_when_central_did_not_answer(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        _serve(page, errors={"error": "store_unavailable"}, errors_status=503)
        connect(page, origin, "attention")
        expect(_room_lights(page)).to_contain_text("Central did not answer with the room lights it is in doubt of.")
        expect(visible_page(page)).not_to_contain_text("Nothing needs attention")


def test_house_lights_says_cant_read_and_link_closed_with_the_last_word_kept(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        _serve(page)
        connect(page, origin, "lights")
        family = page.get_by_role("table", name="In the house", exact=True).get_by_role("row").filter(
            has=page.get_by_role("rowheader", name="Family Room Lights", exact=True))
        expect(family).to_contain_text(re.compile(r"Can't read since \S+ \d\d:\d\d \(keeping Off\)"))
        meter = page.get_by_role("table", name="Sensors in the house", exact=True).get_by_role("row").filter(
            has=page.get_by_role("rowheader", name="Window meter", exact=True))
        expect(meter).to_contain_text(re.compile(r"Link closed since \S+ \d\d:\d\d \(keeping 0 lx\)"))
        assert not re.search(r"home assistant|galleria", visible_page(page).inner_text(), re.I)


def test_the_frame_page_shows_centrals_error_on_overview_apart_from_hardware(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _serve(page)
        connect(page, origin)
        shown = open_frame(page, FRAME, "overview")
        error = shown.get_by_role("region", name="Central's error", exact=True)
        expect(error).to_contain_text("Its Pi reports this Frame is black because Central can't stand behind a room light")
        expect(error).to_contain_text(LINE1)
        expect(error).to_contain_text(LINE2)
        expect(error.get_by_role("link", name="Family Room Lights", exact=True)).to_have_attribute("href", "#/lights")
        expect(error.get_by_role("link", name="See what to do on Needs attention", exact=True)).to_have_attribute(
            "href", "#/attention")
        hardware = open_frame(page, FRAME, "hardware")
        expect(hardware.get_by_role("region", name="Central's error", exact=True)).to_have_count(0)
        expect(hardware).not_to_contain_text(LINE1)


def test_the_frame_page_says_when_its_pi_reports_no_central_error(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        _serve(page, impediment={"frame_id": FRAME, "impediment": {"kind": "clear"}})
        connect(page, origin)
        shown = open_frame(page, FRAME, "overview")
        expect(shown.get_by_role("region", name="Central's error", exact=True)).to_contain_text(
            "Its Pi reports no error of Central's blacks this Frame.")
