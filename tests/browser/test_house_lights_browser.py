"""House › Lights and Settings › Home Assistant on the production app: House › Lights speaks only of
lights, Sensors, colour sensors and readings (no integration, transport, topic or entity name) and
picks a smart light from Home Assistant's light entities, a smart Sensor from its illuminance
entities and a colour sensor from its colour-temperature entities, by name with the entity id as the
detail; a light's colour line says where its colour comes from; Home Assistant's own page says when
Central last heard its states, Galleria's version beside Central's, every entity and the room light,
Sensor or colour sensor it reports, and the states it could not record, with no MQTT or broker word
(GET, PUT and DELETE /v1/operator/house/lights, /sensors and /colour-sensors, GET
/v1/operator/integrations/home-assistant), each refusal said in words.

Central serves the house's lights; Home Assistant's states (HomeAssistantStatesView) are the central
lane's, so until they land here `_states` serves them with the contract's shape.

Mutation probes: render a Sensor's or a light's reference (its entity) on House › Lights -> the
no-integration case fails; the state line worked out from `heard_at` instead of the entry -> the
always-on lamp, never heard, reads "Not reported yet" and the state case fails; a remove that
ignores Central's 409 -> the Sensor's refusal is never said; the picker lists illuminance entities
for a light -> the picker case fails; the colour column shows the light's typed kelvin instead of
Central's colour line -> the colour sensor case never reads "Following Sky colour".
"""

import json
import os
import re

import pytest
from console_tasks import connect, go, visible_page
from operator_harness import operator_server
from playwright.sync_api import expect
from test_frame_picture_browser import _off_thread

from central.infra.frame_lighting_store import PgHouseLock
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import ColourSensorRef, LightStated, ReporterRef
from contracts.room_light import Dimmer, KelvinColour

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

CONSOLE = {"X-Photo-Wall-Console": "1"}
HOME_ASSISTANT = "home-assistant"
WINDOW_METER = {"name": "Window meter", "at": {"source": HOME_ASSISTANT, "address": "sensor.window_illuminance"}}
SPOT = {"kind": "reported", "name": "Bookshelf spot", "reporter": {"source": HOME_ASSISTANT, "address": "light.bookshelf"},
        "colour": {"kind": "its-bulb", "typed_kelvin": 2700}}
# Words House › Lights never shows: the integration, its transport, its topics and its entities.
INTEGRATION_WORDS = re.compile(r"home assistant|mqtt|topic|light\.bookshelf|sensor\.window_illuminance|sensor\.sky_colour",
                               re.I)
HEARD_AT = 1_791_000_000
GALLERIA = {"version": "0.40.0", "kinds": ["light", "illuminance", "colour-temperature"], "heard_at": HEARD_AT,
            "central_version": "0.40.0", "update": False}
# Home Assistant's states as Central last heard them (HomeAssistantStatesView).
STATES = {
    "heard_at": HEARD_AT,
    "entities": [
        {"entity": "light.bookshelf", "kind": "light", "name": "Bookshelf", "reports": "bookshelf-spot", "heard_at": None},
        {"entity": "light.kitchen", "kind": "light", "name": "Kitchen ceiling", "reports": None, "heard_at": HEARD_AT},
        {"entity": "sensor.window_illuminance", "kind": "illuminance", "name": "Window illuminance",
         "reports": "window-meter", "heard_at": HEARD_AT},
        {"entity": "sensor.porch_illuminance", "kind": "illuminance", "name": "Porch illuminance", "reports": None,
         "heard_at": HEARD_AT},
        {"entity": "sensor.sky_colour", "kind": "colour-temperature", "name": "Sky colour temperature", "reports": None,
         "heard_at": HEARD_AT},
    ],
    "galleria": GALLERIA,
    "refused": [{"source": HOME_ASSISTANT, "address": "light.porch", "reason": "unknown_address", "at": HEARD_AT}],
}


def _states(page, view=STATES):
    """Serve Home Assistant's states with the contract's shape (the central lane's route)."""
    page.route("**/v1/operator/integrations/home-assistant", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(view)))


def signed_in(page, origin):
    """Sign in and wait for a section to show: the session cookie is set only once Central answered
    the sign-in, and a write sent before then is refused (401)."""
    connect(page, origin, "lights")


def put(page, origin, path, body):
    """One write through the production app, as the signed-in console sends it."""
    response = page.request.put(origin + path, data=body, headers={**CONSOLE, "Origin": origin})
    assert response.ok, (path, response.status, response.text())
    return response.json()


def _table(page, name):
    return page.get_by_role("table", name=name, exact=True)


def _row(page, table, name):
    return _table(page, table).get_by_role("row").filter(has=page.get_by_role("rowheader", name=name, exact=True))


def _dialog(page, title):
    return page.get_by_role("dialog", name=title, exact=True)


def _add_light(page, name, *, sensor=None, kelvin="2700"):
    """Add a room light a Sensor (its name) measures, or one always on."""
    page.get_by_role("button", name="Add a room light", exact=True).click()
    dialog = _dialog(page, "Add a room light")
    dialog.get_by_role("textbox", name="Name", exact=True).fill(name)
    if sensor is None:
        dialog.get_by_role("radio", name="Always on", exact=True).click()
    else:
        dialog.get_by_role("radio", name="Measured by a Sensor", exact=True).click()
        dialog.get_by_role("combobox", name="Sensor", exact=True).select_option(label=sensor)
    dialog.get_by_role("spinbutton", name="Colour temperature", exact=True).fill(kelvin)
    with page.expect_response(lambda r: "/house/lights/" in r.url and r.request.method == "PUT") as sent:
        dialog.get_by_role("button", name="Add light", exact=True).click()
    return sent.value


def test_house_lights_speaks_only_of_lights_sensors_and_readings(page, registry):
    """Acceptance C1: with a light that reports itself and a Sensor in the smart home, both
    referenced by Home Assistant entities, House › Lights names neither the integration nor its
    entities; a Sensor and the light it measures are set up, a lamp nobody reports is always on, and
    removing the Sensor is refused in words while it measures a light."""
    with operator_server(registry.db, registry.clock) as origin:
        _states(page)
        signed_in(page, origin)
        put(page, origin, "/v1/operator/house/sensors/window-meter", WINDOW_METER)
        put(page, origin, "/v1/operator/house/lights/bookshelf-spot", SPOT)
        go(page, "lights")
        expect(_row(page, "In the house", "Bookshelf spot")).to_contain_text("Reports itself")
        expect(_row(page, "Sensors in the house", "Window meter")).to_contain_text("In the smart home")
        assert not INTEGRATION_WORDS.search(visible_page(page).inner_text()), visible_page(page).inner_text()

        light = _add_light(page, "Daylight", sensor="Window meter", kelvin="6500")
        assert (light.status, light.url.rsplit("/", 1)[-1]) == (200, "daylight"), light.text()
        lamp = _add_light(page, "Reading lamp")
        assert lamp.status == 200, lamp.text()
        daylight = _row(page, "In the house", "Daylight")
        expect(daylight).to_contain_text("Measured by Window meter")
        expect(daylight).to_contain_text("Typed 6,500 K")
        expect(daylight).to_contain_text("Not reported yet")
        expect(daylight).to_contain_text("Never")
        expect(_row(page, "In the house", "Reading lamp")).to_contain_text("On, 100 %")
        expect(_row(page, "In the house", "Reading lamp")).to_contain_text("Always on")
        expect(_row(page, "Sensors in the house", "Window meter")).to_contain_text("Daylight")

        _row(page, "Sensors in the house", "Window meter").get_by_role(
            "button", name="Remove Window meter", exact=True).click()
        dialog = _dialog(page, "Remove Window meter?")
        with page.expect_response(lambda r: "/house/sensors/" in r.url and r.request.method == "DELETE") as sent:
            dialog.get_by_role("button", name="Confirm remove", exact=True).click()
        assert (sent.value.status, sent.value.json()["error"]) == (409, "sensor_reports_a_light")
        expect(dialog).to_contain_text("Not removed: This Sensor measures a room light.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(_row(page, "Sensors in the house", "Window meter")).to_be_visible()
        assert not INTEGRATION_WORDS.search(visible_page(page).inner_text()), visible_page(page).inner_text()


def test_home_assistant_says_when_its_states_were_last_heard_and_what_each_entity_reports(page, registry):
    """Acceptance A4: the Home Assistant page says "States last heard" (or "Not heard yet"), lists each
    entity by name with its entity id and the room light or Sensor it reports, and the state it could
    not record with why; no MQTT or broker word is left, and the picking is House › Lights'."""
    with operator_server(registry.db, registry.clock) as origin:
        _states(page)
        signed_in(page, origin)
        put(page, origin, "/v1/operator/house/sensors/window-meter", WINDOW_METER)
        put(page, origin, "/v1/operator/house/lights/bookshelf-spot", SPOT)
        go(page, "home-assistant")
        shown = visible_page(page)
        expect(shown).to_contain_text("States last heard:")
        expect(_row(page, "From Home Assistant", "Bookshelf")).to_contain_text("light.bookshelf")
        expect(_row(page, "From Home Assistant", "Bookshelf")).to_contain_text("Light: Bookshelf spot")
        expect(_row(page, "From Home Assistant", "Window illuminance")).to_contain_text("Sensor: Window meter")
        expect(_row(page, "From Home Assistant", "Kitchen ceiling")).to_contain_text("Nothing yet")
        expect(_row(page, "Newest first", "light.porch")).to_contain_text(
            "No room light, Sensor or colour sensor is linked to this entity yet. Pick it on House › Lights.")
        expect(shown).to_contain_text(re.compile(r"Versions: Galleria 0\.40\.0, heard \S.* · Central 0\.40\.0"))
        expect(shown).not_to_contain_text("Update Galleria")
        expect(shown.get_by_role("link", name="Room lights and Sensors", exact=True)).to_have_attribute("href", "#/lights")
        assert not re.search(r"mqtt|broker|topic", shown.inner_text(), re.I), shown.inner_text()


def test_home_assistant_not_heard_yet(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        _states(page, {"heard_at": None, "entities": [], "refused": [],
                       "galleria": {**GALLERIA, "version": None, "kinds": [], "heard_at": None, "update": True}})
        signed_in(page, origin)
        go(page, "home-assistant")
        expect(visible_page(page)).to_contain_text("States last heard: Not heard yet")
        expect(visible_page(page)).to_contain_text("Galleria has not been heard from: install it in HACS")


def test_home_assistant_says_to_update_galleria_that_cannot_send_colour_sensors(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        _states(page, {**STATES, "galleria": {**GALLERIA, "version": "0.39.0", "update": True}})
        signed_in(page, origin)
        go(page, "home-assistant")
        shown = visible_page(page)
        expect(shown).to_contain_text(re.compile(r"Versions: Galleria 0\.39\.0, heard \S.* · Central 0\.40\.0"))
        expect(shown).to_contain_text("Update Galleria in HACS: this version cannot send colour sensors yet")


def _options(select):
    return [option.strip() for option in select.locator("option").all_inner_texts()]


def test_the_picker_offers_light_entities_for_a_light_and_illuminance_entities_for_a_sensor(page, registry):
    """Acceptance A5: a light that reports itself picks only from Home Assistant's light entities, a
    smart Sensor only from its illuminance entities, each by name with its entity id; the saved light
    and Sensor name the entity picked, and the light reports itself on House › Lights."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    _off_thread(lambda: lights.record_light_state(ReporterRef(source=HOME_ASSISTANT, address="light.kitchen"),
                                                  LightStated(state=Dimmer(on=True, brightness=0.5), colour=None)))
    with operator_server(registry.db, registry.clock) as origin:
        _states(page)
        signed_in(page, origin)
        go(page, "lights")
        page.get_by_role("button", name="Add a room light", exact=True).click()
        dialog = _dialog(page, "Add a room light")
        dialog.get_by_role("textbox", name="Name", exact=True).fill("Kitchen")
        dialog.get_by_role("radio", name="Reports itself", exact=True).click()
        smart = dialog.get_by_role("combobox", name="Smart light", exact=True)
        assert _options(smart) == ["Bookshelf (light.bookshelf)", "Kitchen ceiling (light.kitchen)"]
        smart.select_option(label="Kitchen ceiling (light.kitchen)")
        with page.expect_response(lambda r: r.url.endswith("/house/lights/kitchen") and r.request.method == "PUT") as sent:
            dialog.get_by_role("button", name="Add light", exact=True).click()
        assert sent.value.status == 200, sent.value.text()
        assert sent.value.request.post_data_json["reporter"] == {"source": HOME_ASSISTANT, "address": "light.kitchen"}
        expect(_row(page, "In the house", "Kitchen")).to_contain_text("Reports itself")
        expect(_row(page, "In the house", "Kitchen")).to_contain_text("On, 50 %")

        page.get_by_role("button", name="Add a smart Sensor", exact=True).click()
        dialog = _dialog(page, "Add a smart Sensor")
        dialog.get_by_role("textbox", name="Name", exact=True).fill("Porch meter")
        meter = dialog.get_by_role("combobox", name="Smart light sensor", exact=True)
        assert _options(meter) == ["Window illuminance (sensor.window_illuminance)",
                                   "Porch illuminance (sensor.porch_illuminance)"]
        meter.select_option(label="Porch illuminance (sensor.porch_illuminance)")
        with page.expect_response(lambda r: "/house/sensors/" in r.url and r.request.method == "PUT") as sent:
            dialog.get_by_role("button", name="Add Sensor", exact=True).click()
        assert (sent.value.status, sent.value.url.rsplit("/", 1)[-1]) == (200, "porch-meter"), sent.value.text()
        assert sent.value.request.post_data_json["at"] == {"source": HOME_ASSISTANT,
                                                           "address": "sensor.porch_illuminance"}
        expect(_row(page, "Sensors in the house", "Porch meter")).to_contain_text("In the smart home")
        assert not INTEGRATION_WORDS.search(visible_page(page).inner_text()), visible_page(page).inner_text()


def test_a_light_follows_a_colour_sensor_and_the_sensor_cannot_go_while_followed(page, registry):
    """Add a colour sensor, picked from Home Assistant's colour-temperature entities only; set daylight
    (measured by the window Sensor) to take its colour from it; once the sky's colour is heard at
    3,400 K, daylight's colour line reads "Following Sky colour 3,400 K" and the colour sensor lists
    daylight as following it; removing the colour sensor is refused in words while daylight follows
    it (409 colour_sensor_followed)."""
    with operator_server(registry.db, registry.clock) as origin:
        _states(page)
        signed_in(page, origin)
        put(page, origin, "/v1/operator/house/sensors/window-meter", WINDOW_METER)
        put(page, origin, "/v1/operator/house/lights/daylight",
            {"kind": "measured", "name": "Daylight", "sensor": "window-meter", "colour": {"kind": "typed", "kelvin": 5500}})
        go(page, "lights")
        expect(_row(page, "In the house", "Daylight")).to_contain_text("Typed 5,500 K")

        page.get_by_role("button", name="Add a colour sensor", exact=True).click()
        dialog = _dialog(page, "Add a colour sensor")
        dialog.get_by_role("textbox", name="Name", exact=True).fill("Sky colour")
        entity = dialog.get_by_role("combobox", name="Smart colour temperature sensor", exact=True)
        assert _options(entity) == ["Sky colour temperature (sensor.sky_colour)"]
        with page.expect_response(lambda r: "/house/colour-sensors/" in r.url and r.request.method == "PUT") as sent:
            dialog.get_by_role("button", name="Add colour sensor", exact=True).click()
        assert (sent.value.status, sent.value.url.rsplit("/", 1)[-1]) == (200, "sky-colour"), sent.value.text()
        assert sent.value.request.post_data_json == {
            "name": "Sky colour", "at": {"source": HOME_ASSISTANT, "address": "sensor.sky_colour"}}
        expect(_row(page, "Colour sensors in the house", "Sky colour")).to_contain_text("Not reported yet")

        _row(page, "In the house", "Daylight").get_by_role("button", name="Change Daylight", exact=True).click()
        dialog = _dialog(page, "Change Daylight")
        dialog.get_by_role("radio", name="A colour sensor", exact=True).click()
        dialog.get_by_role("combobox", name="Colour sensor", exact=True).select_option(label="Sky colour")
        with page.expect_response(lambda r: r.url.endswith("/house/lights/daylight") and r.request.method == "PUT") as sent:
            dialog.get_by_role("button", name="Save light", exact=True).click()
        assert sent.value.status == 200, sent.value.text()
        assert sent.value.request.post_data_json["colour"] == {"kind": "colour-sensor", "sensor": "sky-colour",
                                                               "typed_kelvin": 5500}
        expect(_row(page, "In the house", "Daylight")).to_contain_text("Typed 5,500 K until Sky colour says")

        lights = PgRoomLights(registry.db, PgHouseLock())
        _off_thread(lambda: lights.record_colour_reading(
            ColourSensorRef(source=HOME_ASSISTANT, address="sensor.sky_colour"), KelvinColour(kelvin=3400)))
        expect(_row(page, "In the house", "Daylight")).to_contain_text("Following Sky colour 3,400 K")
        sky = _row(page, "Colour sensors in the house", "Sky colour")
        expect(sky).to_contain_text("3,400 K")
        expect(sky).to_contain_text("Daylight")

        sky.get_by_role("button", name="Remove Sky colour", exact=True).click()
        dialog = _dialog(page, "Remove Sky colour?")
        with page.expect_response(lambda r: "/house/colour-sensors/" in r.url and r.request.method == "DELETE") as sent:
            dialog.get_by_role("button", name="Confirm remove", exact=True).click()
        assert (sent.value.status, sent.value.json()["error"]) == (409, "colour_sensor_followed")
        expect(dialog).to_contain_text("Not removed: A room light takes its colour from this colour sensor.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(_row(page, "Colour sensors in the house", "Sky colour")).to_be_visible()
        assert not INTEGRATION_WORDS.search(visible_page(page).inner_text()), visible_page(page).inner_text()
