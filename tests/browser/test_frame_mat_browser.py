"""The Frame's mat and its lights on the Position tab (print look, design R5, R6; owner, 2026-10-12:
"the mat shadow is definitely a Frame quality"; "cool white light coming from the side AND warm
white coming from the top"), live through Position's session: the draft is the Frame's position and
its mat, every edit carries both, Done saves both and Revert returns both.

As in test_console_frame_page_browser.py, a real session needs a Node presenting it, so a fake
Central answers the session routes with Central's rules (`FakeSessions`); this one adds Central's
mat rules (central/fleet/node_calibration.py): a session begins at the saved mat or the draft's, an
`edit` or `renew` without `mat` is refused (422 `trial_mat_required`), one the contract refuses
(contracts.models.Mat) is refused (422 `trial_mat_invalid`), and Save writes the mat with the
calibration through the real Registry's Save CAS. The reload reads the Frame's mat from the real
inventory.

Mutation probes: omit `mat` from an edit (liveAdjustment.js `payload()`) -> trial_mat_required, and
the tracer case never reaches Done; replace instead of append on Add (frame-mat.tsx) -> the
two-lights case saves one light; send strength as a float (1.5, not 15 tenths) -> trial_mat_invalid,
and the bounds case fails.
"""

import os

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from pydantic import ValidationError
from test_console_frame_page_browser import FRAME, PRESENTED, FakeSessions, _bar, _seed

from central.registry import RegistryError
from central.transaction_locks import acquire_runtime_locks
from contracts.models import Calibration, Mat

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NEW_LIGHT = {"from_degrees": 0, "steep_degrees": 45, "softness_degrees": 10, "strength_tenths": 10,
             "colour_kelvin": 6500}


def _saved_mat(registry):
    return next(frame for frame in registry.inventory().frames if frame.id == FRAME).mat


def _save(registry, calibration, mat, expected_revision):
    """Central's Save: the trial's calibration and mat through the Registry's Save CAS."""
    with registry.db.transaction() as conn:
        acquire_runtime_locks(conn)
        row = conn.execute("SELECT f.generation, f.configuration_revision, b.player_id, b.output_id, "
                           "p.authority_epoch FROM frames f JOIN bindings b ON b.frame_id=f.id "
                           "JOIN players p ON p.id=b.player_id WHERE f.id=%s", (FRAME,)).fetchone()
        registry.commit_calibration_trial_in(
            conn, frame_id=FRAME, player_id=row["player_id"], authority_epoch=row["authority_epoch"],
            output_id=row["output_id"], binding_generation=row["generation"],
            config_revision=row["configuration_revision"], calibration_revision=expected_revision,
            calibration=calibration, mat=mat)


class MatSessions(FakeSessions):
    """FakeSessions with Central's mat rules (see the module docstring); `mats` lists every mat the
    page sent, in order: with an edit or renew, and with a begin for a changed draft (a change
    made while no session runs, e.g. right after Done, rides the next session's begin)."""

    def __init__(self, page, registry):
        self.mats = []
        self._draft_mat = None
        super().__init__(page, registry)

    def _candidate(self):
        super()._candidate()
        saved = _saved_mat(self.registry).model_dump(mode="json")
        self.row["mat"] = saved if self._draft_mat is None else self._draft_mat

    def _answer(self, route):
        body = route.request.post_data_json or {}
        operation = body.get("operation", "begin")
        if operation in ("edit", "renew"):
            if "mat" not in body:
                self.sent.append(operation)
                self.bodies.append(body)
                route.fulfill(status=422, json={"error": "trial_mat_required"})
                return
        if "mat" in body:
            self.mats.append(body["mat"])
            try:
                Mat.model_validate(body["mat"])
            except ValidationError:
                self.sent.append(operation)
                self.bodies.append(body)
                route.fulfill(status=422, json={"error": "trial_mat_invalid"})
                return
        if operation == "save" and self.row is not None and self.row["state"] == "active":
            self._save(route)
            return
        self._draft_mat = body.get("mat")
        super()._answer(route)

    def _save(self, route):
        self.sent.append("save")
        self.bodies.append(route.request.post_data_json or {})
        if self.row["presented_sequence"] != self.row["sequence"]:
            route.fulfill(status=409, json={"error": "trial_latest_not_presented"})
            return
        try:
            _save(self.registry, Calibration.model_validate(self.row["calibration"]),
                  Mat.model_validate(self.row["mat"]), self.row["calibration_revision"])
        except RegistryError as error:
            route.fulfill(status=409, json={"error": error.code})
            return
        self.row.update(state="saved", saved_calibration=self._saved(), saved_mat=self.row["mat"])
        route.fulfill(json=dict(self.row, touched_at=1000.0, hard_expires_at=1000.0 + self.window))


@pytest.fixture
def sessions(page, registry):
    yield MatSessions(page, registry)
    page.unroute_all(behavior="ignoreErrors")


def _mat(page):
    return page.get_by_role("region", name="Position", exact=True).get_by_role("group", name="Mat", exact=True)


def _light(page, number):
    return _mat(page).get_by_role("group", name=f"Light {number}", exact=True)


def _wait_for_mat(page, sessions, check):
    """Waits until a mat the page sent passes `check`; returns it."""
    for _ in range(50):
        sent = [mat for mat in sessions.mats if check(mat)]
        if sent:
            return sent[-1]
        page.wait_for_timeout(100)
    raise AssertionError((sessions.mats, list(zip(sessions.sent, sessions.bodies))[-5:]))


def _done(page):
    bar = _bar(page)
    expect(bar.get_by_role("status")).to_have_text(PRESENTED)
    bar.get_by_role("button", name="Done", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()


def test_a_light_and_a_depth_ride_the_position_session_and_done_saves_them(page, registry, sessions):
    """R5 tracer: add a light, set the depth to 2.0 mm: the session's edit carries the mat with the
    position; Done saves it, and after a reload the Position tab shows the saved mat.

    Mutation: omit `mat` from the edit -> Central's 422 trial_mat_required, Done never comes."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        mat = _mat(page)
        expect(mat).to_contain_text("No lights: no shadow.")
        mat.get_by_role("button", name="Add a light", exact=True).click()
        mat.get_by_role("slider", name="Depth", exact=True).fill("2")
        expect(mat).to_contain_text("2.0 mm")
        sent = _wait_for_mat(page, sessions, lambda mat: mat["depth_mm"] == 2 and len(mat["lights"]) == 1)
        assert sent == {"depth_mm": 2, "lights": [NEW_LIGHT]}, sent
        body = sessions.bodies[sessions.sent.index("edit")]
        assert set(body) == {"operation", "expected_sequence", "calibration", "mat"}, body
        _done(page)
        saved = _saved_mat(registry)
        assert saved.depth_mm == 2.0 and [light.model_dump() for light in saved.lights] == [NEW_LIGHT]

        page.reload()
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_mat(page).get_by_role("slider", name="Depth", exact=True)).to_have_value("2")
        expect(_light(page, 1)).to_be_visible()
        expect(_bar(page).get_by_role("button", name="Done", exact=True)).to_be_disabled()  # nothing changed


def test_two_lights_of_different_colours_and_directions_keep_their_order(page, registry, sessions):
    """R6: a cool light from the right and a warm one from the top are kept in order and saved.

    Mutation: replace instead of append on Add -> one light saved."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        mat = _mat(page)
        mat.get_by_role("slider", name="Depth", exact=True).fill("2.5")
        add = mat.get_by_role("button", name="Add a light", exact=True)
        add.click()
        _light(page, 1).get_by_role("slider", name="Comes from", exact=True).fill("90")
        _light(page, 1).get_by_role("slider", name="Colour", exact=True).fill("7500")
        expect(_light(page, 1)).to_contain_text("90° · 3 o'clock")
        add.click()
        _light(page, 2).get_by_role("slider", name="Colour", exact=True).fill("2700")
        _wait_for_mat(page, sessions, lambda mat: len(mat["lights"]) == 2 and mat["lights"][1]["colour_kelvin"] == 2700)
        _done(page)
        saved = _saved_mat(registry)
        assert saved.depth_mm == 2.5
        assert [(light.from_degrees, light.colour_kelvin) for light in saved.lights] == [(90, 7500), (0, 2700)]

        mat.get_by_role("button", name="Remove light 1", exact=True).click()
        _wait_for_mat(page, sessions, lambda mat: len(mat["lights"]) == 1)
        _bar(page).get_by_role("button", name="Revert", exact=True).click()
        expect(_light(page, 2)).to_be_visible()  # back to the saved two
        assert len(_saved_mat(registry).lights) == 2


def test_add_stops_at_four_lights_and_strength_is_sent_in_tenths(page, registry, sessions):
    """Bounds: Add is disabled at four lights; strength shows one decimal and is sent as tenths.

    Mutation: send strength as a float (1.5) -> Central's 422 trial_mat_invalid."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        mat = _mat(page)
        add = mat.get_by_role("button", name="Add a light", exact=True)
        for _ in range(4):
            add.click()
        expect(add).to_be_disabled()
        expect(mat).to_contain_text("A mat takes up to 4 lights.")
        _light(page, 1).get_by_role("slider", name="Strength", exact=True).fill("1.5")
        expect(_light(page, 1)).to_contain_text("1.5")
        sent = _wait_for_mat(page, sessions, lambda mat: mat["lights"] and mat["lights"][0]["strength_tenths"] == 15)
        assert len(sent["lights"]) == 4
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
