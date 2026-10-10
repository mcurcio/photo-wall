"""The Frame page at /console: one page per Frame, reached in one click from its Wall tile, with
Overview, Position, Picture and Hardware tabs (pages/frame-page.tsx).

The live adjustment (liveAdjustment.js) talks to Central's calibration session routes. A real
session needs a Node's Display Host exchanging with Central (tests/test_node_calibration.py
proves that half, keepalive included), so here a fake Central answers those routes with the
same rules: a session begins at the saved calibration, an edit raises its sequence and waits
for the Pi to show it, `keepalive` (and only an edit or `keepalive`) slides its idle deadline,
which IDLE_SECONDS shortens, and `save` commits through the real Registry only once the Pi has
shown the latest change. The rest of the page is the real server.
"""

import itertools
import os
import re
import time

import pytest
from console_tasks import open_frame
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_registry import enroll

from central.registry import FrameCreate, RegistryError
from contracts.models import Calibration, FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "hall-frame"
OUTPUT = "HDMI-A-1"
GAIN = 1.5
# The fake's idle window: shorter than Central's 5 s, so a page that stopped keeping its session
# would see it end within the test.
IDLE_SECONDS = 1.5


LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
# Distinct from the 1920 x 1080 the Player app reports, so a Frame profile fact and the Panel
# record can be told apart.
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)


def _seed(registry, profile=LANDSCAPE, rotation=0):
    """A Frame bound to an Output the Player app reported connected at 1920 x 1080, with a
    saved calibration (gain 1.5); returns the Player's id."""
    identity, _key, _request = enroll(registry, count=1)
    landscape = profile.width_px > profile.height_px
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100, width_mm=500 if landscape else 300,
        height_mm=300 if landscape else 500, profile=profile))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    registry.calibrate(FRAME, "commit", expected_revision=1,
                       calibration=Calibration(gain=GAIN, rotation=rotation), expected_generation=1)
    return identity["player_id"]


class FakeSessions:
    """Central's calibration session routes, answered with Central's rules (see the module
    docstring). `shown` is whether the Pi shows each new change on its next exchange; `sent`
    lists every operation the page sent, in order."""

    def __init__(self, page, registry):
        self.registry = registry
        self.shown = True
        self.sent = []
        self.row = None
        self.touched = 0.0
        self.ids = itertools.count(1)
        page.route("**/v1/operator/frames/*/calibration-capability",
                   lambda route: route.fulfill(json={"mode": "native_trial"}))
        page.route("**/v1/operator/frames/*/calibration-trials**", self._answer)

    def _saved(self):
        frame = next(frame for frame in self.registry.inventory().frames if frame.id == FRAME)
        return frame.calibration.model_dump(mode="json")

    def _candidate(self):
        self.row.update(candidate_sha256=f"{self.row['sequence']:064x}",
                        presented_sequence=None, presented_sha256=None)

    def _present(self):
        if self.shown:
            self.row.update(presented_sequence=self.row["sequence"],
                            presented_sha256=self.row["candidate_sha256"])

    def _answer(self, route):
        body = route.request.post_data_json or {}
        operation = body.get("operation", "begin")
        self.sent.append(operation)
        now = time.monotonic()
        if self.row is not None and self.row["state"] == "active" and now - self.touched > IDLE_SECONDS:
            self.row["state"] = "expired"
        if operation == "begin":
            if self.row is not None and self.row["state"] == "active":
                route.fulfill(status=409, json={"error": "trial_already_active"})
                return
            saved = self._saved()
            self.row = {"trial_id": f"00000000-0000-4000-8000-{next(self.ids):012d}", "state": "active",
                        "sequence": 1, "calibration": saved, "calibration_revision": saved["revision"],
                        "touched_at": 1000.0, "hard_expires_at": 1110.0}
            self._candidate()
            self.touched = now
        elif self.row["state"] != "active":
            pass  # a finished session answers as it ended
        elif operation in ("keepalive", "status"):
            if operation == "keepalive":
                self.touched = now  # only `keepalive` (or an edit) keeps the session
            self._present()  # the Pi's next exchange shows the latest change
        elif operation == "edit":
            assert body["expected_sequence"] == self.row["sequence"]
            self.row.update(sequence=self.row["sequence"] + 1, calibration=body["calibration"])
            self._candidate()
            self.touched = now
        elif operation == "save":
            if self.row["presented_sequence"] != self.row["sequence"]:
                route.fulfill(status=409, json={"error": "trial_latest_not_presented"})
                return
            calibration = Calibration.model_validate(self.row["calibration"])
            try:  # Central's Save is a compare-and-set on the revision the session began at
                self.registry.calibrate(FRAME, "commit", expected_revision=self.row["calibration_revision"],
                                        calibration=calibration, expected_generation=1)
            except RegistryError as error:
                route.fulfill(status=409, json={"error": error.code})
                return
            self.row["state"] = "saved"
        elif operation == "end":
            self.row["state"] = "ended"
        route.fulfill(json=dict(self.row))

    def count(self, operation):
        return self.sent.count(operation)


@pytest.fixture
def sessions(page, registry):
    """The fake Central's session routes on `page`, taken off before the registry closes (the
    page's last requests, as it unloads, then fail as they would against a stopped server)."""
    yield FakeSessions(page, registry)
    page.unroute_all(behavior="ignoreErrors")


def test_one_click_on_a_wall_tile_opens_the_frame_page_and_its_position_is_kept_live(page, registry, sessions):
    """Wall -> the Frame's tile -> its page -> Position -> nudge -> Done.

    The session stays open while the tab is shown: the page keeps it (`keepalive`) past the fake
    Central's idle window, so it is never begun again. Done waits for the Pi to show the latest
    change, then saves it through the real Registry; leaving the tab ends the session.

    Mutation: send `status` instead of `keepalive` in liveAdjustment.js -> the session ends at
    the idle window and is begun again -> the `begin` count assertion goes RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        fake = sessions

        # One click on the tile opens the Frame's page at Overview.
        page.get_by_role("button", name=f"Frame {FRAME}", exact=True).click()
        expect(page.get_by_role("heading", level=2, name=f"Frame {FRAME}", exact=True)).to_be_focused()
        assert page.evaluate("location.hash") == f"#/wall/frames/{FRAME}/overview"
        tabs = page.get_by_role("tablist", name="Frame settings", exact=True)
        expect(tabs.get_by_role("tab")).to_have_text(["Overview", "Position", "Picture", "Hardware"])

        tabs.get_by_role("tab", name="Position", exact=True).click()
        assert page.evaluate("location.hash") == f"#/wall/frames/{FRAME}/position"
        live = page.get_by_role("group", name="Show on the Display", exact=True)
        expect(live.get_by_role("status")).to_have_text("The Pi is showing the saved position.")
        done = live.get_by_role("button", name="Done", exact=True)
        expect(done).to_be_disabled()  # nothing changed yet

        # Nudge the top-left corner 10 px right; the Pi has not shown it yet.
        fake.shown = False
        page.get_by_label("Move", exact=True).select_option(label="Top-left corner")
        page.get_by_role("button", name="Move right: the top-left corner", exact=True).click()
        expect(page.get_by_label("Top-left x", exact=True)).to_have_value("10")
        expect(live.get_by_role("status")).to_have_text("Sending your change to the Display…")
        expect(done).to_be_disabled()

        fake.shown = True  # the Pi's next exchange shows it
        expect(live.get_by_role("status")).to_have_text(
            "The Pi is showing your change. Press Done to keep it, or Revert to undo it.")
        # Kept past the idle window, never begun again.
        page.wait_for_timeout(IDLE_SECONDS * 2 * 1000)
        assert fake.count("begin") == 1, fake.sent
        assert fake.count("keepalive") >= 3, fake.sent

        done.click()
        expect(live.get_by_role("status")).to_have_text("Saved. The Pi is showing the saved position.")
        saved = next(frame for frame in registry.inventory().frames if frame.id == FRAME).calibration
        assert saved.corners[0][0] == pytest.approx(10 / 1920)
        assert saved.gain == GAIN

        # Leaving the tab ends the session.
        ended = fake.count("end")
        tabs.get_by_role("tab", name="Overview", exact=True).click()
        deadline = time.monotonic() + 5
        while fake.count("end") == ended and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert fake.count("end") > ended, fake.sent


def test_brightness_is_on_the_picture_tab_in_plain_words(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        brightness = frame.get_by_role("slider", name="Brightness — Photo Wall picture adjustment", exact=True)
        expect(brightness).to_have_value(str(GAIN))
        expect(brightness).to_have_attribute("aria-valuetext", "150 %")


def test_every_tab_speaks_plain_words(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        for tab in ("overview", "position", "picture", "hardware"):
            frame = open_frame(page, FRAME, tab)
            expect(frame).not_to_contain_text(re.compile(r"SDR gain|lease|trial|acknowledg|claimed|derived", re.I))


def test_a_pi_that_cannot_show_changes_live_says_so_and_offers_no_controls(page, registry):
    """The real server: an enrolled Player without Display Host answers `legacy_preview`."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "position")
        expect(frame.get_by_role("status")).to_contain_text(
            "This Frame's Pi runs software that cannot show changes live")
        expect(frame.get_by_role("link", name="Open the Pi's software page", exact=True)).to_have_attribute(
            "href", re.compile(r"^#/players/device-"))
        expect(frame.get_by_role("img", name="Picture position")).to_have_count(0)
        expect(frame.get_by_role("button", name="Done")).to_have_count(0)


def test_an_unbound_frame_is_sent_to_its_hardware_tab(page, registry):
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100, width_mm=300, height_mm=500, profile=PORTRAIT))
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        for tab in ("position", "picture"):
            frame = open_frame(page, FRAME, tab)
            expect(frame.get_by_role("status")).to_have_text(
                "Choose which Pi and HDMI output feed this Frame first (Hardware tab).")


def test_hardware_keeps_the_frame_profile_apart_from_the_panel_record(page, registry):
    """The diagonal (24 in) is a Frame profile fact: it shows under Frame profile, never in the
    Panel record from the Player app's last enrollment (console DDD §19).

    Mutation: render a Frame profile field inside the Panel group -> the not_to_contain_text
    assertion goes RED."""
    player_id = _seed(registry, PORTRAIT)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        expect(frame.get_by_role("region", name="Frame profile", exact=True)).to_contain_text("24")
        expect(frame.get_by_role("link", name=player_id, exact=True)).to_have_attribute(
            "href", re.compile(r"^#/players/device-"))
        expect(frame).to_contain_text(OUTPUT)
        panel = frame.get_by_role("group", name="Panel at the Player app's last enrollment (may be stale)")
        expect(panel).to_contain_text(
            "Player app reported Panel connected at the Player app's last enrollment (may be stale)")
        expect(panel).to_contain_text("Output resolution at that enrollment: 1920 × 1080")
        expect(panel).not_to_contain_text("24")


def _mismatch(frame):
    return frame.get_by_role("status").filter(
        has_text="The Player app reported 1920 × 1080 at its last enrollment")


def test_the_frame_profile_notes_a_reported_resolution_it_does_not_match(page, registry):
    _seed(registry, PORTRAIT)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        warning = _mismatch(frame)
        expect(warning).to_contain_text("this record may be stale")
        expect(warning).to_contain_text("restart the Player app if the Display changed")
        expect(warning).to_contain_text("The Frame profile is 1080 × 1920")
        expect(warning).to_contain_text("The saved rotation 0° was considered")
        expect(warning).to_contain_text("unbind this Frame, edit its profile, then bind it and set its position")
        stored = next(frame for frame in registry.inventory().frames if frame.id == FRAME)
        assert (stored.profile.width_px, stored.profile.height_px) == (1080, 1920)


@pytest.mark.parametrize(("profile", "rotation"), [(LANDSCAPE, 0), (PORTRAIT, 90)])
def test_no_note_when_the_reported_resolution_matches_as_mounted(page, registry, profile, rotation):
    _seed(registry, profile, rotation)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        expect(frame.get_by_role("region", name="Frame profile", exact=True)).to_be_visible()
        expect(_mismatch(frame)).to_have_count(0)


def test_a_rebound_frame_does_not_trust_its_old_rotation(page, registry):
    player_id = _seed(registry, PORTRAIT, rotation=90)
    registry.unbind(FRAME, expected_generation=1)
    registry.bind(FRAME, player_id, OUTPUT, expected_generation=2)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        expect(_mismatch(frame)).to_contain_text("This Frame's position is not set for this Pi yet")


# --- The Position editor: every assertion is on a visible value, word or request, never on
# SVG coordinates (a drag ACTION may use coordinates).


def test_dragging_a_corner_inward_moves_the_picture(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        fake = sessions
        frame = open_frame(page, FRAME, "position")
        live = frame.get_by_role("group", name="Show on the Display", exact=True)
        expect(live.get_by_role("status")).to_have_text("The Pi is showing the saved position.")
        editor = frame.get_by_role("img", name="Picture position", exact=True)
        editor.scroll_into_view_if_needed()
        box = editor.bounding_box()
        scale = box["width"] / 512  # the drawing is 480 wide inside a 16 px margin
        page.mouse.move(box["x"] + 16 * scale, box["y"] + 16 * scale)
        page.mouse.down()
        page.mouse.move(box["x"] + 76 * scale, box["y"] + 76 * scale, steps=6)
        page.mouse.up()
        expect(frame.get_by_label("Top-left x", exact=True)).not_to_have_value("0")
        expect(live.get_by_role("status")).to_have_text(
            "The Pi is showing your change. Press Done to keep it, or Revert to undo it.")
        assert fake.count("edit") >= 1
        expect(frame.get_by_role("alert")).to_have_count(0)


@pytest.mark.parametrize(("corner", "values"), [
    # Top-left to (0.9, 0.9) of the screen folds the picture.
    ("Top-left", {"x": "1728", "y": "972"}),
    # Bottom-right 5e-7 of the height from the top: a sliver the server would refuse (its
    # 1e-6 epsilon, convex.js). Mutation: set convex.js EPSILON to 0 -> no message, RED.
    ("Bottom-right", {"y": "0.00054"}),
])
def test_a_folded_or_thin_picture_is_refused_where_it_is_typed(page, registry, sessions, corner, values):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        fake = sessions
        frame = open_frame(page, FRAME, "position")
        live = frame.get_by_role("group", name="Show on the Display", exact=True)
        expect(live.get_by_role("status")).to_have_text("The Pi is showing the saved position.")
        y = frame.get_by_label(f"{corner} y", exact=True)
        before = y.input_value()
        for axis, value in values.items():
            frame.get_by_label(f"{corner} {axis}", exact=True).fill(value)
        expect(frame.get_by_role("alert")).to_have_text("corners must form a convex aperture")
        # The refused value snaps back and never reaches the Display.
        expect(y).to_have_value(before)
        page.wait_for_timeout(500)
        refused = float(values["y"]) / 1080
        assert all(abs(point[1] - refused) > 1e-9 for point in fake.row["calibration"]["corners"]), fake.row


def test_a_refresh_never_overwrites_the_draft(page, registry, sessions):
    """Two planes: the draft is the operator's; a refresh replaces the snapshot, never it.

    Mutation: have useDraft re-seed from the saved calibration on refresh -> the slider jumps
    to 1.2 after Refresh -> RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        brightness = frame.get_by_role("slider", name="Brightness — Photo Wall picture adjustment", exact=True)
        brightness.focus()
        for _ in range(8):  # 1.5 -> 1.9 in 0.05 steps
            page.keyboard.press("ArrowRight")
        expect(brightness).to_have_value("1.9")

        # The saved calibration moves underneath the open draft (a save from elsewhere).
        registry.calibrate(FRAME, "commit", expected_revision=2,
                           calibration=Calibration(gain=1.2), expected_generation=1)
        page.get_by_role("button", name="Refresh", exact=True).click()
        page.wait_for_timeout(500)
        expect(brightness).to_have_value("1.9")
        # Revert returns to the saved calibration as refreshed: the refresh was real.
        frame.get_by_role("button", name="Revert", exact=True).click()
        expect(brightness).to_have_value("1.2")


def test_a_stale_done_is_refused_and_keeps_the_other_save(page, registry, sessions):
    """Done is a compare-and-set on the calibration the session began from: a save made
    elsewhere meanwhile wins, the page says so, and Revert loads it and carries on."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        live = frame.get_by_role("group", name="Show on the Display", exact=True)
        brightness = frame.get_by_role("slider", name="Brightness — Photo Wall picture adjustment", exact=True)
        brightness.focus()
        page.keyboard.press("ArrowLeft")  # 1.5 -> 1.45
        expect(live.get_by_role("status")).to_have_text(
            "The Pi is showing your change. Press Done to keep it, or Revert to undo it.")

        registry.calibrate(FRAME, "commit", expected_revision=2,
                           calibration=Calibration(gain=1.2), expected_generation=1)
        live.get_by_role("button", name="Done", exact=True).click()
        expect(live.get_by_role("alert")).to_have_text(re.compile("Someone saved a different position or picture"))
        assert next(f for f in registry.inventory().frames if f.id == FRAME).calibration.gain == 1.2

        live.get_by_role("button", name="Revert", exact=True).click()
        expect(brightness).to_have_value("1.2")
        expect(live.get_by_role("alert")).to_have_count(0)
        expect(live.get_by_role("status")).to_have_text("The Pi is showing the saved picture.")
