"""House › Lights and Settings › Home Assistant on the production app: House › Lights speaks only of
lights, Sensors and readings (no integration, transport, topic or entity name), and Home Assistant's
own page shows its link, every entity and the room light or Sensor it reports, links an entity to a
light, and lists the messages it could not record (GET, PUT and DELETE /v1/operator/house/lights and
/sensors, GET /v1/operator/integrations/home-assistant), each refusal said in words.

Mutation probes: render a Sensor's or a light's reference (its entity) on House › Lights -> the
no-integration case fails; the state line worked out from `heard_at` instead of the entry -> the
always-on lamp, never heard, reads "Not reported yet" and the state case fails; a remove that
ignores Central's 409 -> the Sensor's refusal is never said; Link that sends the entity on the
light's name -> the light is not "Reports itself".
"""

import os
import re

import pytest
from console_tasks import connect, go, visible_page
from operator_harness import operator_server
from playwright.sync_api import expect
from test_frame_picture_browser import _off_thread

from central.infra.frame_lighting_store import PgHouseLock
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import ReporterRef, ReportRefusal
from contracts.room_light import Dimmer

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

CONSOLE = {"X-Photo-Wall-Console": "1"}
HOME_ASSISTANT = "home-assistant"
WINDOW_METER = {"name": "Window meter", "at": {"source": HOME_ASSISTANT, "address": "sensor.window_illuminance"}}
SPOT = {"kind": "reported", "name": "Bookshelf spot", "reporter": {"source": HOME_ASSISTANT, "address": "light.bookshelf"},
        "colour": {"kind": "fixed", "kelvin": 2700}}
# Words House › Lights never shows: the integration, its transport, its topics and its entities.
INTEGRATION_WORDS = re.compile(r"home assistant|mqtt|topic|light\.bookshelf|sensor\.window_illuminance", re.I)


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
        expect(daylight).to_contain_text("6500 K")
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


def test_home_assistant_shows_its_link_entities_and_refused_messages_and_links_a_light(page, registry):
    """Acceptance C1's other half: the Home Assistant page says the link's state, lists the entity
    the house names and the one Home Assistant reported that nothing names yet, and the message it
    could not record with why; linking the kitchen entity to the always-on Kitchen light makes that
    light report itself on House › Lights."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    _off_thread(lambda: lights.record_light_state(ReporterRef(source=HOME_ASSISTANT, address="light.kitchen"),
                                                  Dimmer(on=True, brightness=0.5)))
    _off_thread(lambda: lights.record_refused(HOME_ASSISTANT, "photowall/light/light.porch", ReportRefusal.UNKNOWN_ADDRESS))
    with operator_server(registry.db, registry.clock) as origin:
        signed_in(page, origin)
        put(page, origin, "/v1/operator/house/lights/bookshelf-spot", SPOT)
        put(page, origin, "/v1/operator/house/lights/kitchen",
            {"kind": "fixed", "name": "Kitchen", "colour": {"kind": "fixed", "kelvin": 3000}})
        go(page, "home-assistant")
        shown = visible_page(page)
        expect(shown).to_contain_text("Link:")
        expect(_row(page, "From Home Assistant", "light.bookshelf")).to_contain_text("Light: Bookshelf spot")
        kitchen = _row(page, "From Home Assistant", "light.kitchen")
        expect(kitchen).to_contain_text("Nothing yet")
        expect(_row(page, "Newest first", "photowall/light/light.porch")).to_contain_text(
            "No room light or Sensor is linked to this entity yet.")

        kitchen.get_by_role("button", name="Link light.kitchen", exact=True).click()
        dialog = _dialog(page, "Link light.kitchen")
        dialog.get_by_role("combobox", name="Room light", exact=True).select_option(label="Kitchen")
        with page.expect_response(lambda r: r.url.endswith("/house/lights/kitchen") and r.request.method == "PUT") as sent:
            dialog.get_by_role("button", name="Link", exact=True).click()
        assert sent.value.status == 200, sent.value.text()
        assert sent.value.request.post_data_json["reporter"] == {"source": HOME_ASSISTANT, "address": "light.kitchen"}
        expect(_row(page, "From Home Assistant", "light.kitchen")).to_contain_text("Light: Kitchen")

        go(page, "lights")
        expect(_row(page, "In the house", "Kitchen")).to_contain_text("Reports itself")
        expect(_row(page, "In the house", "Kitchen")).to_contain_text("On, 50 %")
        assert not INTEGRATION_WORDS.search(visible_page(page).inner_text()), visible_page(page).inner_text()
        expect(visible_page(page)).not_to_contain_text("light.kitchen")
