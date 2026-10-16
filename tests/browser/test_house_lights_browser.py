"""Settings › Lights (decision 0028): the house's room lights, Sensors and Home Assistant link, set
up through the page on the production app (GET, PUT and DELETE /v1/operator/house/lights and
/sensors, central/room_light_routes.py), each refusal said in words.

Mutation probes: the state line worked out from `heard_at` instead of `state` (room-lights.tsx's
"Now" column) -> the always-on lamp, never heard, reads "Not reported yet" and the state case
fails; a remove that ignores Central's 409 -> the Sensor's refusal is never said and the Sensor
leaves the list; the id not derived from the name -> Central's 422 and the light is never listed.
"""

import os

import pytest
from console_tasks import go
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)


def open_lights(page, origin):
    """Sign in and open Settings › Lights by its sidebar link."""
    sign_in(page, origin)
    go(page, "lights")


def _table(page, name):
    return page.get_by_role("table", name=name, exact=True)


def _row(page, table, name):
    return _table(page, table).get_by_role("row").filter(has=page.get_by_role("rowheader", name=name, exact=True))


def _dialog(page, title):
    return page.get_by_role("dialog", name=title, exact=True)


def _add_sensor(page, name, entity):
    page.get_by_role("button", name="Add a Sensor", exact=True).click()
    dialog = _dialog(page, "Add a Sensor")
    dialog.get_by_role("textbox", name="Name", exact=True).fill(name)
    dialog.get_by_role("radio", name="Home Assistant", exact=True).click()
    dialog.get_by_role("textbox", name="Home Assistant entity", exact=True).fill(entity)
    with page.expect_response(lambda r: "/house/sensors/" in r.url and r.request.method == "PUT") as sent:
        dialog.get_by_role("button", name="Add Sensor", exact=True).click()
    return sent.value


def _add_light(page, name, *, sensor=None, kelvin="2700"):
    """Add a room light reported by `sensor` (its name), or always on."""
    page.get_by_role("button", name="Add a room light", exact=True).click()
    dialog = _dialog(page, "Add a room light")
    dialog.get_by_role("textbox", name="Name", exact=True).fill(name)
    if sensor is None:
        dialog.get_by_role("radio", name="Always on", exact=True).click()
    else:
        dialog.get_by_role("radio", name="A Sensor", exact=True).click()
        dialog.get_by_role("combobox", name="Sensor", exact=True).select_option(label=sensor)
    dialog.get_by_role("spinbutton", name="Colour temperature", exact=True).fill(kelvin)
    with page.expect_response(lambda r: "/house/lights/" in r.url and r.request.method == "PUT") as sent:
        dialog.get_by_role("button", name="Add light", exact=True).click()
    return sent.value


def test_a_sensor_and_the_light_it_reports_are_set_up_and_the_sensor_stays_while_it_reports(page, registry):
    """Acceptance 2: create a Sensor and a room light it reports, and a lamp nobody reports; each
    light's line is its state's ("Not reported yet" for the Sensor's light, never heard; "On, 100 %"
    for the lamp, also never heard); removing the Sensor is refused in words and it stays; once its
    light is removed, so can it be."""
    with operator_server(registry.db, registry.clock) as origin:
        open_lights(page, origin)
        expect(page.get_by_text("No Sensors.", exact=False)).to_be_visible()
        expect(page.get_by_text("never facing the wall", exact=False)).to_be_visible()

        sensor = _add_sensor(page, "Window meter", "sensor.window_illuminance")
        assert (sensor.status, sensor.url.rsplit("/", 1)[-1]) == (200, "window-meter"), sensor.text()
        expect(_row(page, "Light meters", "Window meter")).to_contain_text("Not reported yet")

        light = _add_light(page, "Daylight", sensor="Window meter", kelvin="6500")
        assert (light.status, light.url.rsplit("/", 1)[-1]) == (200, "daylight"), light.text()
        lamp = _add_light(page, "Reading lamp")
        assert lamp.status == 200, lamp.text()
        daylight = _row(page, "In the house", "Daylight")
        expect(daylight).to_contain_text("Sensor: Window meter")
        expect(daylight).to_contain_text("6500 K")
        expect(daylight).to_contain_text("Not reported yet")
        expect(daylight).to_contain_text("Never")
        expect(_row(page, "In the house", "Reading lamp")).to_contain_text("On, 100 %")
        expect(_row(page, "In the house", "Reading lamp")).to_contain_text("Always on")
        expect(_row(page, "Light meters", "Window meter")).to_contain_text("Daylight")

        _row(page, "Light meters", "Window meter").get_by_role("button", name="Remove Window meter", exact=True).click()
        dialog = _dialog(page, "Remove Window meter?")
        with page.expect_response(lambda r: "/house/sensors/" in r.url and r.request.method == "DELETE") as sent:
            dialog.get_by_role("button", name="Confirm remove", exact=True).click()
        assert (sent.value.status, sent.value.json()) == (409, {"error": "sensor-reports-a-light"})
        expect(dialog).to_contain_text("Not removed: This Sensor reports a room light.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(_row(page, "Light meters", "Window meter")).to_be_visible()

        daylight.get_by_role("button", name="Remove Daylight", exact=True).click()
        _dialog(page, "Remove Daylight?").get_by_role("button", name="Confirm remove", exact=True).click()
        expect(_row(page, "In the house", "Daylight")).to_have_count(0)
        _row(page, "Light meters", "Window meter").get_by_role("button", name="Remove Window meter", exact=True).click()
        _dialog(page, "Remove Window meter?").get_by_role("button", name="Confirm remove", exact=True).click()
        expect(_row(page, "Light meters", "Window meter")).to_have_count(0)
        expect(page.get_by_role("status").filter(has_text="Removed Window meter.")).to_be_visible()


def test_a_reported_colour_on_an_always_on_light_cannot_be_chosen_and_a_bad_entity_is_said(page, registry):
    """Only a Home Assistant light reports its colour: the choice is offered for it alone. An entity
    that is not Home Assistant's form is said before anything is sent."""
    with operator_server(registry.db, registry.clock) as origin:
        open_lights(page, origin)
        page.get_by_role("button", name="Add a room light", exact=True).click()
        dialog = _dialog(page, "Add a room light")
        dialog.get_by_role("textbox", name="Name", exact=True).fill("Overheads")
        expect(dialog.get_by_role("radio", name="Reported by the bulb", exact=True)).to_be_visible()
        dialog.get_by_role("textbox", name="Home Assistant entity", exact=True).fill("Living Room")
        dialog.get_by_role("button", name="Add light", exact=True).click()
        expect(dialog).to_contain_text("Type the light's Home Assistant entity, as light.bookshelf.")
        dialog.get_by_role("radio", name="Always on", exact=True).click()
        expect(dialog.get_by_role("radio", name="Reported by the bulb", exact=True)).to_have_count(0)
