"""A Frame's lighting on the Position tab (decision 0028; owner, 2026-10-15: "each frame. the whole
wall might share a trigger, but each configuration will be unique"; "live on the wall"): its mat
depth, its lights (each one Frame's view of a room light, in lux with its reference) and its paper,
live through Position's session: every edit carries the whole lighting with the position, Done
saves both and Revert returns both.

The room lights are the real ones: the test sets them up through Settings › Lights' routes
(central/room_light_routes.py, PUT /v1/operator/house/lights and /sensors) on the production app,
and the editor's room light picker reads them back. As in test_console_frame_page_browser.py, a
real session needs a Node presenting it, so a fake Central answers the session routes with
Central's rules (`LightingSessions`); this one adds Central's lighting rules
(central/fleet/node_calibration.py): an `edit` or `renew` without `lighting` is refused (422
`trial_lighting_required`), one the contract refuses (contracts.frame_lighting.FrameLighting) is
refused (`trial_lighting_invalid`), a light naming no room light is refused
(`trial_lighting_unknown_room_light`) and one whose reference does not match its room light's
reporter (`trial_lighting_reference_mismatch`: a Sensor's light takes a lux reference, any other
DIMMER_AT_FULL); Save writes the lighting with the calibration through the real Registry's Save
CAS, and the reload reads the Frame's lighting from the real inventory.

Mutation probes: send `mat` instead of `lighting` (liveAdjustment.js `payload()`) ->
trial_lighting_required, and the tracer case never reaches Done; keep DIMMER_AT_FULL when a Sensor's
light is chosen (frame-lighting.tsx `referenceFor`) -> the mismatch case's refusal never clears;
replace instead of append on Add -> the two-lights case saves one light.
"""

import os
import re

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from pydantic import ValidationError
from test_console_frame_page_browser import FRAME, PRESENTED, FakeSessions, _bar, _seed

from central.infra.room_light_store import room_lights_in
from central.registry import RegistryError
from central.room_light.policy import check_frame_lighting
from central.transaction_locks import acquire_runtime_locks
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

CONSOLE = {"X-Photo-Wall-Console": "1"}
SPOT = {"name": "Bookshelf spot", "reporter": {"kind": "home-assistant-light", "entity": "light.bookshelf"},
        "colour": {"kind": "fixed", "kelvin": 2700}}
WINDOW_METER = {"name": "Window meter", "at": {"kind": "home-assistant", "entity": "sensor.window_illuminance"}}
DAYLIGHT = {"name": "Daylight", "reporter": {"kind": "sensor", "sensor": "window-meter"},
            "colour": {"kind": "fixed", "kelvin": 6500}}
MISMATCH = "A light's reference does not match how its room light reports"


def put(page, origin, path, body):
    """One Settings › Lights write through the production app, as the signed-in console sends it."""
    response = page.request.put(origin + path, data=body, headers={**CONSOLE, "Origin": origin})
    assert response.ok, (path, response.status, response.text())
    return response.json()


def room_lights(page, origin, *, daylight=False):
    """The bookshelf spotlight (a Home Assistant light), and with `daylight` a window Sensor and the
    daylight it reports. Waits for the signed-in shell first: the writes ride its session cookie."""
    expect(page.get_by_role("navigation", name="Sections", exact=True)).to_be_visible()
    put(page, origin, "/v1/operator/house/lights/bookshelf-spot", SPOT)
    if daylight:
        put(page, origin, "/v1/operator/house/sensors/window-meter", WINDOW_METER)
        put(page, origin, "/v1/operator/house/lights/daylight", DAYLIGHT)
    listed = page.request.get(origin + "/v1/operator/house/lights", headers=CONSOLE)
    assert listed.ok, listed.text()


def saved_lighting(registry):
    return next(frame for frame in registry.inventory().frames if frame.id == FRAME).lighting


def _save(registry, calibration, lighting, expected_revision):
    """Central's Save: the trial's calibration and lighting through the Registry's Save CAS."""
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        row = conn.execute("SELECT f.generation, f.configuration_revision, b.player_id, b.output_id, "
                           "p.authority_epoch FROM frames f JOIN bindings b ON b.frame_id=f.id "
                           "JOIN players p ON p.id=b.player_id WHERE f.id=%s", (FRAME,)).fetchone()
        registry.commit_calibration_trial_in(
            conn, frame_id=FRAME, player_id=row["player_id"], authority_epoch=row["authority_epoch"],
            output_id=row["output_id"], binding_generation=row["generation"],
            config_revision=row["configuration_revision"], calibration_revision=expected_revision,
            calibration=calibration, lighting=lighting, session_id=None, restored_from=None)


class LightingSessions(FakeSessions):
    """FakeSessions with Central's lighting rules (see the module docstring): the contract, then
    central/room_light/policy.py `check_frame_lighting` against the room lights stored now (the seeded
    fixed "room" and whatever the test put through Settings › Lights); `lightings` lists every
    lighting the page sent, in order."""

    def __init__(self, page, registry):
        self.lightings = []
        self._draft = None
        super().__init__(page, registry)

    def _candidate(self):
        super()._candidate()
        saved = saved_lighting(self.registry).model_dump(mode="json")
        self.row["lighting"] = saved if self._draft is None else self._draft

    def _refuse(self, route, operation, body, code):
        self.sent.append(operation)
        self.bodies.append(body)
        route.fulfill(status=422, json={"error": code})

    def _refusal(self, lighting):
        try:
            draft = FrameLighting.model_validate(lighting)
        except ValidationError:
            return "trial_lighting_invalid"
        with self.registry.db.transaction() as conn:
            refusal = check_frame_lighting(draft, room_lights_in(conn))
        return None if refusal is None else refusal.value

    def _answer(self, route):
        body = route.request.post_data_json or {}
        operation = body.get("operation", "begin")
        if operation in ("edit", "renew") and "lighting" not in body:
            self._refuse(route, operation, body, "trial_lighting_required")
            return
        if "lighting" in body:
            self.lightings.append(body["lighting"])
            refusal = self._refusal(body["lighting"])
            if refusal is not None:
                self._refuse(route, operation, body, refusal)
                return
        if operation == "save" and self.row is not None and self.row["state"] == "active":
            self._save(route)
            return
        self._draft = body.get("lighting")
        super()._answer(route)

    def _save(self, route):
        self.sent.append("save")
        self.bodies.append(route.request.post_data_json or {})
        if self.row["presented_sequence"] != self.row["sequence"]:
            route.fulfill(status=409, json={"error": "trial_latest_not_presented"})
            return
        try:
            _save(self.registry, Calibration.model_validate(self.row["calibration"]),
                  FrameLighting.model_validate(self.row["lighting"]), self.row["calibration_revision"])
        except RegistryError as error:
            route.fulfill(status=409, json={"error": error.code})
            return
        self.row.update(state="saved", saved_calibration=self._saved(), saved_lighting=self.row["lighting"])
        route.fulfill(json=dict(self.row, touched_at=1000.0, hard_expires_at=1000.0 + self.window))


@pytest.fixture
def sessions(page, registry):
    yield LightingSessions(page, registry)
    page.unroute_all(behavior="ignoreErrors")


def lighting_group(page):
    return page.get_by_role("region", name="Position", exact=True).get_by_role("group", name="Lighting", exact=True)


def light_group(page, number):
    return lighting_group(page).get_by_role("group", name=f"Light {number}", exact=True)


def wait_for_lighting(page, sessions, check):
    """Waits until a lighting the page sent passes `check`; returns it."""
    for _ in range(50):
        sent = [lighting for lighting in sessions.lightings if check(lighting)]
        if sent:
            return sent[-1]
        page.wait_for_timeout(100)
    raise AssertionError((sessions.lightings, list(zip(sessions.sent, sessions.bodies))[-5:]))


def done(page):
    bar = _bar(page)
    expect(bar.get_by_role("status")).to_have_text(PRESENTED)
    bar.get_by_role("button", name="Done", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()


def add_close_light(page, room_light="Bookshelf spot"):
    """Add a light, make it a Close light of `room_light`; returns its group."""
    lighting = lighting_group(page)
    lighting.get_by_role("button", name="Add a light", exact=True).click()
    count = lighting.get_by_role("group", name=re.compile(r"^Light \d$")).count()
    light = light_group(page, count)
    light.get_by_role("radio", name="Close light", exact=True).click()
    light.get_by_role("combobox", name="Room light", exact=True).select_option(label=room_light)
    return light


def test_a_close_lights_lux_rides_the_session_done_keeps_it_and_revert_puts_it_back(page, registry, sessions):
    """Acceptance 1 tracer: add a close light of the bookshelf spot and type its lux, 800: the
    session's next edit carries `lighting` (with the position) holding that lux, and the trial view
    Central answers holds it; Done saves it, and after a reload the tab shows it. A later lux of 1200,
    reverted, puts the saved 800 back and saves nothing.

    Mutation: send `mat` instead of `lighting` -> Central's 422 trial_lighting_required, Done never comes."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        room_lights(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        light = add_close_light(page)
        number = lighting_group(page).get_by_role("group", name=re.compile(r"^Light \d$")).count()
        light.get_by_role("spinbutton", name="Lux at the Frame", exact=True).fill("800")
        sent = wait_for_lighting(page, sessions, lambda lighting: any(
            each["kind"] == "close" and each["lux"] == 800 for each in lighting["lights"]))
        close = next(each for each in sent["lights"] if each["kind"] == "close")
        assert (close["light"], close["reference"]) == ("bookshelf-spot", {"kind": "at-full"}), close
        edit = sessions.bodies[sessions.sent.index("edit")]
        assert set(edit) == {"operation", "expected_sequence", "calibration", "lighting"}, edit
        assert any(each["lux"] == 800 for each in sessions.row["lighting"]["lights"])  # the trial view
        done(page)
        saved = saved_lighting(registry)
        assert [(each.kind, each.light, each.lux) for each in saved.lights][-1] == ("close", "bookshelf-spot", 800)

        page.reload()
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        lux = light_group(page, number).get_by_role("spinbutton", name="Lux at the Frame", exact=True)
        expect(lux).to_have_value("800")
        expect(_bar(page).get_by_role("button", name="Done", exact=True)).to_be_disabled()  # nothing changed

        lux.fill("1200")
        wait_for_lighting(page, sessions, lambda lighting: any(each["lux"] == 1200 for each in lighting["lights"]))
        _bar(page).get_by_role("button", name="Revert", exact=True).click()
        expect(lux).to_have_value("800")
        assert saved_lighting(registry) == saved


def test_a_reference_mismatch_is_said_in_words_and_choosing_the_sensors_reading_clears_it(page, registry, sessions):
    """A close light first of the bookshelf spot (read at full), then of daylight, which the window
    Sensor reports: the editor gives it the Sensor's reading as its reference only once the Sensor
    has one; while it has none, Central's trial_lighting_reference_mismatch is said in words, and
    typing what the Sensor read clears it.

    Mutation: keep DIMMER_AT_FULL after a typed reading -> the refusal never clears."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        room_lights(page, origin, daylight=True)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        light = add_close_light(page, "Daylight")
        expect(light).to_contain_text("Its Sensor has no reading")
        position = page.get_by_role("region", name="Position", exact=True)
        expect(position).to_contain_text(MISMATCH)
        assert any(each["light"] == "daylight" and each["reference"] == {"kind": "at-full"}
                   for lighting in sessions.lightings for each in lighting["lights"]), sessions.lightings

        light.get_by_role("spinbutton", name="Its Sensor read", exact=True).fill("320")
        wait_for_lighting(page, sessions, lambda lighting: any(each["reference"] == 320 for each in lighting["lights"]))
        expect(position).not_to_contain_text(MISMATCH)
        done(page)
        assert [each.reference for each in saved_lighting(registry).lights][-1] == 320


def test_two_lights_keep_their_order_and_the_paper_rides_with_them(page, registry, sessions):
    """Two close lights of the spot and the Matte preset: kept in order and saved with the paper; Add
    stops at four lights.

    Mutation: replace instead of append on Add -> one light saved."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        room_lights(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        before = len(saved_lighting(registry).lights)
        add_close_light(page).get_by_role("spinbutton", name="Lux at the Frame", exact=True).fill("500")
        add_close_light(page).get_by_role("spinbutton", name="Lux at the Frame", exact=True).fill("250")
        lighting = lighting_group(page)
        lighting.get_by_role("button", name="Matte (1.6)", exact=True).click()
        wait_for_lighting(page, sessions, lambda sent: len(sent["lights"]) == before + 2
                          and sent["lights"][-1]["lux"] == 250 and sent["paper"]["ink_density"] == 1.6)
        done(page)
        saved = saved_lighting(registry)
        assert [each.lux for each in saved.lights][-2:] == [500, 250]
        assert saved.paper.ink_density == 1.6

        add = lighting.get_by_role("button", name="Add a light", exact=True)
        while lighting.get_by_role("group", name=re.compile(r"^Light \d$")).count() < 4:
            add.click()
        expect(add).to_be_disabled()
        expect(lighting).to_contain_text("A Frame takes up to 4 lights.")
