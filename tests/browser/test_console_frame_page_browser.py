"""The Frame page at /console: one page per Frame, reached in one click from its Wall tile, with
Overview, Position, Picture and Hardware tabs (pages/frame-page.tsx).

The live adjustment (liveAdjustment.js) talks to Central's calibration session routes. A real
session needs a Node's Display Host exchanging with Central (tests/test_node_calibration.py
proves that half, keepalive and renew included), so here a fake Central answers those routes
with the same rules: a session begins at the saved calibration, or at a draft whose base is the
saved revision; an edit carries the draft's base and raises its sequence, then waits for the Pi
to show it; `keepalive` (and only an edit or `keepalive`) slides its idle deadline, which
IDLE_SECONDS shortens; `renew` ends it and begins the next at the draft in one step; a session
ends at its hard deadline (`window`, which a test may shorten); and `save` commits through the
real Registry only once the Pi has shown the latest change. A draft whose base is not the saved
revision is refused (`trial_baseline_revision_changed`). The rest of the page is the real server.
"""

import itertools
import os
import re
import time

import pytest
from console_tasks import open_frame, visit
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
DEVICE = "device-frame-page-pi"  # its Player id ends "d89500"
GAIN = 1.5
# The fake's idle window: shorter than Central's 5 s, so a page that stopped keeping its session
# would see it end within the test.
IDLE_SECONDS = 1.5
# A served hard window just over the page's RENEW_BEFORE_S (15 s): the page hands each session
# over about a second after it begins, so a test sees several handovers while the tab is open.
SHORT_WINDOW = 16
PRESENTED = re.compile(r"^Presented by the Pi · \d\d:\d\d:\d\d$")
WAITING = "Previewing — waiting for the Pi"
OVERTAKEN = "Someone saved a different position or picture for this Frame meanwhile"
BRIGHTNESS = "Brightness — Photo Wall picture adjustment"


LANDSCAPE = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24)
# Distinct from the 1920 x 1080 the Player app reports, so a Frame profile fact and the Display
# record can be told apart.
PORTRAIT = FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)


def _seed(registry, profile=LANDSCAPE, rotation=0):
    """A Frame bound to an Output the Player app reported connected at 1920 x 1080, with a
    saved calibration (gain 1.5, revision 2); returns the Player's id."""
    # A fixed device id gives a fixed Player id: the page shows its last six hex digits, and a
    # random one held "24" about one run in fifty, which the Hardware tab's diagonal check reads.
    identity, _key, _request = enroll(registry, count=1, device_id=DEVICE)
    landscape = profile.width_px > profile.height_px
    registry.create_frame(FrameCreate(
        id=FRAME, surface_id="wall", x_mm=100, y_mm=100, width_mm=500 if landscape else 300,
        height_mm=300 if landscape else 500, profile=profile))
    registry.bind(FRAME, identity["player_id"], OUTPUT, expected_generation=0)
    registry.calibrate(FRAME, "commit", expected_revision=1,
                       calibration=Calibration(gain=GAIN, rotation=rotation), expected_generation=1)
    return identity["player_id"]


def _saved(registry):
    return next(frame for frame in registry.inventory().frames if frame.id == FRAME).calibration


def _save_elsewhere(registry, gain=1.2):
    """Another window saves the Frame's calibration (revision 2 -> 3)."""
    registry.calibrate(FRAME, "commit", expected_revision=2, calibration=Calibration(gain=gain),
                       expected_generation=1)


class FakeSessions:
    """Central's calibration session routes, answered with Central's rules (see the module
    docstring). `shown` is whether the Pi shows each new change on its next exchange; `sent`
    lists every operation the page sent, in order; `began_with` the calibration each session
    began at (by `begin` or `renew`); `fail` maps an operation to one refusal to answer it with
    next."""

    def __init__(self, page, registry, window=110):
        self.registry = registry
        self.window = window
        self.shown = True
        self.sent = []
        self.bodies = []
        self.began_with = []
        self.fail = {}
        self.hold_end = False
        self.held = []
        self.refused_busy = 0
        self.row = None
        self.touched = 0.0
        self.began = 0.0
        self.ids = itertools.count(1)
        page.route("**/v1/operator/frames/*/calibration-capability",
                   lambda route: route.fulfill(json={"mode": "native_trial"}))
        page.route("**/v1/operator/frames/*/calibration-trials**", self._answer)

    def _saved(self):
        return _saved(self.registry).model_dump(mode="json")

    def _candidate(self):
        self.row.update(candidate_sha256=f"{next(self.ids):064x}", presented_sequence=None,
                        presented_sha256=None, presented_at=None)

    def _present(self):
        if self.shown and self.row["presented_sha256"] != self.row["candidate_sha256"]:
            self.row.update(presented_sequence=self.row["sequence"],
                            presented_sha256=self.row["candidate_sha256"], presented_at=time.time())

    def _open(self, now, calibration):
        """The Frame's next session, at `calibration` (a draft with its base) or the saved one;
        None when the draft's base is not the saved revision."""
        saved = self._saved()
        if calibration is not None and calibration["revision"] != saved["revision"]:
            return None
        start = saved if calibration is None else calibration
        self.row = {"trial_id": f"00000000-0000-4000-8000-{next(self.ids):012d}", "state": "active",
                    "sequence": 1, "calibration": start, "calibration_revision": start["revision"]}
        self._candidate()
        self.began_with.append(start)
        self.touched = self.began = now
        return self.row

    def state_now(self):
        """The session's state as Central would judge it now."""
        self._lapse(time.monotonic())
        return None if self.row is None else self.row["state"]

    def _lapse(self, now):
        if self.row is not None and self.row["state"] == "active" and (
                now - self.touched > IDLE_SECONDS or now - self.began >= self.window):
            self.row["state"] = "expired"

    def _answer(self, route):
        body = route.request.post_data_json or {}
        operation = body.get("operation", "begin")
        if operation == "end" and self.hold_end:
            self.held.append(route)  # answered (and applied) by `release`
            return
        self.sent.append(operation)
        self.bodies.append(body)
        now = time.monotonic()
        self._lapse(now)
        refusal = self.fail.pop(operation, None)
        if refusal is not None:
            route.fulfill(status=refusal[0], json={"error": refusal[1]})
            return
        if operation == "begin":
            if self.row is not None and self.row["state"] == "active":
                self.refused_busy += 1
                route.fulfill(status=409, json={"error": "trial_already_active"})
                return
            if self._open(now, body.get("calibration")) is None:
                route.fulfill(status=409, json={"error": "trial_baseline_revision_changed"})
                return
        elif self.row["state"] != "active":
            pass  # a finished session answers as it ended
        elif operation in ("keepalive", "status"):
            if operation == "keepalive":
                self.touched = now  # only `keepalive` (or an edit) keeps the session
            self._present()  # the Pi's next exchange shows the latest change
        elif operation in ("edit", "renew"):
            if body["expected_sequence"] != self.row["sequence"]:
                route.fulfill(status=409, json={"error": "trial_sequence_conflict"})
                return
            if body["calibration"]["revision"] != self.row["calibration_revision"]:
                route.fulfill(status=409, json={"error": "trial_baseline_revision_changed"})
                return
            if operation == "renew":
                ended = self.row
                if self._open(now, body["calibration"]) is None:
                    self.row = ended
                    route.fulfill(status=409, json={"error": "trial_baseline_revision_changed"})
                    return
                ended["state"] = "ended"
            else:
                self.row.update(sequence=self.row["sequence"] + 1, calibration=body["calibration"])
                self._candidate()
                self.touched = now
        elif operation == "save":
            if self.row["presented_sequence"] != self.row["sequence"]:
                route.fulfill(status=409, json={"error": "trial_latest_not_presented"})
                return
            calibration = Calibration.model_validate(self.row["calibration"])
            try:  # Central's Save is a compare-and-set on the draft's base revision
                self.registry.calibrate(
                    FRAME, "commit", expected_revision=self.row["calibration_revision"],
                    calibration=calibration, expected_generation=1)
            except RegistryError as error:
                route.fulfill(status=409, json={"error": error.code})
                return
            self.row.update(state="saved", saved_calibration=self._saved())
        elif operation == "end":
            self.row["state"] = "ended"
        # Central's clock: only the difference between the two is the page's to read.
        served = dict(self.row, touched_at=1000.0 + (self.touched - self.began),
                      hard_expires_at=1000.0 + self.window)
        route.fulfill(json=served)

    def count(self, operation):
        return self.sent.count(operation)

    def release(self):
        """Answer, and apply, every `end` held so far."""
        held, self.held, self.hold_end = self.held, [], False
        for route in held:
            self._answer(route)


@pytest.fixture
def sessions(page, registry):
    """The fake Central's session routes on `page`, taken off before the registry closes (the
    page's last requests, as it unloads, then fail as they would against a stopped server)."""
    yield FakeSessions(page, registry)
    page.unroute_all(behavior="ignoreErrors")


@pytest.fixture
def handovers(page, registry):
    """The fake Central with a short served window, so the page hands over while it is open."""
    yield FakeSessions(page, registry, window=SHORT_WINDOW)
    page.unroute_all(behavior="ignoreErrors")


def _bar(page):
    return page.get_by_role("group", name="Show on the Display", exact=True)


def _brighten(page, presses=-1):
    """Change Brightness by `presses` steps of 5 % (negative dims) on the Picture tab."""
    slider = page.get_by_role("slider", name=BRIGHTNESS, exact=True)
    slider.focus()
    for _ in range(abs(presses)):
        page.keyboard.press("ArrowRight" if presses > 0 else "ArrowLeft")
    return slider


def _set_visibility(page, state):
    page.evaluate("""(state) => {
        Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
        document.dispatchEvent(new Event("visibilitychange"));
    }""", state)


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

        # One click on the tile opens the Frame's page at Overview, named by its own heading.
        page.get_by_role("button", name=f"Frame {FRAME}", exact=True).click()
        expect(page.get_by_role("heading", level=1, name=f"Frame {FRAME}", exact=True)).to_be_focused()
        expect(page.get_by_role("heading", level=1)).to_have_count(1)
        assert page.evaluate("location.hash") == f"#/wall/frames/{FRAME}/overview"
        tabs = page.get_by_role("tablist", name="Frame settings", exact=True)
        expect(tabs.get_by_role("tab")).to_have_text(["Overview", "Position", "Picture", "Hardware"])

        tabs.get_by_role("tab", name="Position", exact=True).click()
        assert page.evaluate("location.hash") == f"#/wall/frames/{FRAME}/position"
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        done = bar.get_by_role("button", name="Done", exact=True)
        expect(done).to_be_disabled()  # nothing changed yet
        expect(bar).to_contain_text("No changes to keep.")

        # Nudge the top-left corner 10 px right; the Pi has not shown it yet.
        fake.shown = False
        page.get_by_label("Move", exact=True).select_option(label="Top-left corner")
        page.get_by_role("button", name="Move right: the top-left corner", exact=True).click()
        page.get_by_role("button", name="Exact values", exact=True).click()
        expect(page.get_by_label("Top-left x", exact=True)).to_have_value("10")
        expect(bar.get_by_role("status")).to_have_text(WAITING)
        expect(done).to_be_disabled()
        expect(bar).to_contain_text("Done is offered once the Pi presents your latest change.")

        fake.shown = True  # the Pi's next exchange shows it
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(done).to_be_enabled()
        # Kept past the idle window, never begun again.
        page.wait_for_timeout(IDLE_SECONDS * 2 * 1000)
        assert fake.count("begin") == 1, fake.sent
        assert fake.count("keepalive") >= 3, fake.sent

        done.click()
        expect(page.get_by_role("status").filter(has_text="Saved at")).to_have_text(
            re.compile(r"^Saved at \d\d:\d\d:\d\d\.$"))
        saved = _saved(registry)
        assert saved.corners[0][0] == pytest.approx(10 / 1920)
        assert saved.gain == GAIN

        # The next session shows the saved position; leaving the tab (nothing unkept: no
        # question) ends it.
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        assert fake.state_now() == "active"
        ended = fake.count("end")
        tabs.get_by_role("tab", name="Overview", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        deadline = time.monotonic() + 5
        while fake.count("end") == ended and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert fake.count("end") > ended, fake.sent


def test_brightness_is_on_the_picture_tab_in_plain_words(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        brightness = frame.get_by_role("slider", name=BRIGHTNESS, exact=True)
        expect(brightness).to_have_value("150")
        expect(brightness).to_have_attribute("aria-valuetext", "150 %")


def test_every_tab_speaks_plain_words(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        for tab in ("overview", "position", "picture", "hardware"):
            frame = open_frame(page, FRAME, tab)
            expect(frame).not_to_contain_text(re.compile(
                r"SDR gain|lease|trial|acknowledg|claimed|derived|\bRun\b|\bPanel\b|\bPlayer\b|\blayer\b|"
                r"Surface|enrollment|Unbind", re.I))


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
    Display the Pi reported when its photo app last started (console DDD §19).

    Mutation: render a Frame profile field inside the Pi and HDMI port section -> the
    not_to_contain_text assertion goes RED."""
    player_id = _seed(registry, PORTRAIT)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        expect(frame.get_by_role("region", name="Frame profile", exact=True)).to_contain_text("24")
        port = frame.get_by_role("region", name="Pi and HDMI port", exact=True)
        expect(port.get_by_role("link", name=re.compile(r"^Pi "))).to_have_attribute(
            "href", re.compile(r"^#/players/device-"))
        expect(port).to_contain_text("Fed by: Pi")
        expect(port).to_contain_text(f"Pi {player_id[-6:]} · HDMI 1")  # the port, not "HDMI-A-1"
        expect(port).not_to_contain_text(OUTPUT)
        expect(port).to_contain_text("Display, when the Pi last started: Connected, 1920 × 1080")
        expect(port).not_to_contain_text("24")
        # Central's own words are under Details.
        port.get_by_role("button", name="Details", exact=True).click()
        expect(port).to_contain_text(
            "Player app reported Panel connected at the Player app's last enrollment (may be stale)")


def _mismatch(frame):
    return frame.get_by_role("group", name="Check the Display", exact=True)


def test_the_frame_profile_notes_a_reported_resolution_it_does_not_match(page, registry):
    _seed(registry, PORTRAIT)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "hardware")
        problem = _mismatch(frame)
        expect(problem).to_contain_text("Frame profile: Check the Display.")
        expect(problem).to_contain_text(
            "The Pi reported its Display at 1920 × 1080, but this Frame's profile is 1080 × 1920.")
        expect(problem).to_contain_text("The saved rotation 0° was considered")
        # One action: edit the profile; the how-to is under Details.
        problem.get_by_role("button", name="Edit Frame profile", exact=True).click()
        expect(frame.get_by_role("form", name="Edit Frame profile", exact=True)).to_be_visible()
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
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        editor = frame.get_by_role("img", name="Picture position", exact=True)
        editor.scroll_into_view_if_needed()
        box = editor.bounding_box()
        scale = box["width"] / 512  # the drawing is 480 wide inside a 16 px margin
        page.mouse.move(box["x"] + 16 * scale, box["y"] + 16 * scale)
        page.mouse.down()
        page.mouse.move(box["x"] + 76 * scale, box["y"] + 76 * scale, steps=6)
        page.mouse.up()
        frame.get_by_role("button", name="Exact values", exact=True).click()
        expect(frame.get_by_label("Top-left x", exact=True)).not_to_have_value("0")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(bar.get_by_role("button", name="Done", exact=True)).to_be_enabled()
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
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        frame.get_by_role("button", name="Exact values", exact=True).click()
        y = frame.get_by_label(f"{corner} y", exact=True)
        before = y.input_value()
        for axis, value in values.items():
            frame.get_by_label(f"{corner} {axis}", exact=True).fill(value)
        expect(frame.get_by_role("alert")).to_contain_text("corners must form a convex aperture")
        # The refused value snaps back and never reaches the Display.
        expect(y).to_have_value(before)
        page.wait_for_timeout(500)
        refused = float(values["y"]) / 1080
        assert all(abs(point[1] - refused) > 1e-9 for point in fake.row["calibration"]["corners"]), fake.row


def test_a_refresh_never_overwrites_the_draft(page, registry, sessions):
    """Two planes: the draft is the operator's; a refresh replaces the snapshot, never it.

    Mutation: have useDraft re-seed from the saved calibration on refresh -> the slider jumps
    to 120 after Refresh -> RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "picture")
        brightness = _brighten(page, 8)  # 150 -> 190 %
        expect(brightness).to_have_value("190")

        # The saved calibration moves underneath the open draft (a save from elsewhere).
        _save_elsewhere(registry)
        page.get_by_role("button", name="Refresh", exact=True).click()
        page.wait_for_timeout(500)
        expect(brightness).to_have_value("190")
        # Revert returns to the saved calibration as refreshed: the refresh was real.
        _bar(page).get_by_role("button", name="Revert", exact=True).click()
        expect(brightness).to_have_value("120")


def test_a_stale_done_is_refused_and_keeps_the_other_save(page, registry, sessions):
    """Done is a compare-and-set on the draft's base: a save made elsewhere meanwhile wins, the
    page says so, and Revert loads it and carries on."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        bar = _bar(page)
        brightness = _brighten(page)  # 150 -> 145 %
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)

        _save_elsewhere(registry)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(frame.get_by_role("alert")).to_contain_text(OVERTAKEN)
        assert _saved(registry).gain == 1.2

        frame.get_by_role("alert").get_by_role("button", name="Revert", exact=True).click()
        expect(brightness).to_have_value("120")
        expect(frame.get_by_role("alert")).to_have_count(0)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)


# --- The draft outlives its sessions, with its base: one class fix (liveAdjustment.js, Central's
# `renew` and `begin` with a draft) for every way a session ends while the draft is unkept.
# Each test is RED with the draft's base taken from the session instead of the draft (the
# defect reproduced in review), GREEN with it.


def test_a_draft_resumed_after_the_page_was_hidden_never_overwrites_a_save_made_meanwhile(
        page, registry, sessions):
    """Hidden: the session ends at once and the Display shows the saved values. Shown again:
    the page begins with the draft and its base, and Central refuses it because someone saved
    meanwhile, so the other save is never overwritten.

    Mutation (the reviewed defect): begin at the saved calibration and send the session's
    revision with the draft -> the draft is re-based on the other save and Done overwrites it
    -> RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        bar = _bar(page)
        _brighten(page)  # 150 -> 145 %
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(bar.get_by_role("button", name="Done", exact=True)).to_be_enabled()

        _set_visibility(page, "hidden")
        deadline = time.monotonic() + 5
        while sessions.state_now() == "active" and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert sessions.state_now() == "ended", sessions.sent
        _save_elsewhere(registry)
        _set_visibility(page, "visible")

        expect(frame.get_by_role("alert")).to_contain_text(OVERTAKEN)
        expect(bar.get_by_role("button", name="Done", exact=True)).to_be_disabled()
        page.wait_for_timeout(1000)
        assert _saved(registry).gain == 1.2
        assert sessions.bodies[sessions.sent.index("begin", 1)]["calibration"]["revision"] == 2


def test_a_handover_carries_the_draft_and_never_overwrites_a_save_made_meanwhile(page, registry, handovers):
    """A handover before the hard deadline (`renew`) carries the draft and its base: after a
    save elsewhere, Central refuses it, the page says so and the other save stands.

    Mutation (the reviewed defect): hand over by ending the session and beginning the next at
    the saved calibration -> the draft is re-based on the other save and Done overwrites it
    -> RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        bar = _bar(page)
        _brighten(page)  # 150 -> 145 %
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)

        _save_elsewhere(registry)
        expect(frame.get_by_role("alert")).to_contain_text(OVERTAKEN)
        expect(bar.get_by_role("button", name="Done", exact=True)).to_be_disabled()
        page.wait_for_timeout(1000)
        assert _saved(registry).gain == 1.2


def test_the_session_is_handed_over_with_the_draft_while_the_tab_stays_open(page, registry, handovers):
    """The served window is SHORT_WINDOW, so the page hands the session over about every
    second: by `renew`, never by ending it and beginning another, and every session after the
    first begins at the draft. The Display never shows the saved values in between, Done stays
    available and saves the draft.

    Mutation: never hand over (`left < RENEW_BEFORE_S` -> false) -> no `renew` and the session
    reaches its hard deadline -> RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        _brighten(page)  # 150 -> 145 %
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(4000)
        sent = list(handovers.sent)
        assert sent.count("renew") >= 2, sent
        assert sent.count("begin") == 1 and "end" not in sent, sent
        drafts = [began["gain"] for began in handovers.began_with[1:]]
        assert drafts and all(gain == 1.45 for gain in drafts[1:]), drafts
        assert handovers.state_now() == "active"
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _saved(registry).gain == 1.45


def test_a_closed_page_stops_keeping_its_session_and_it_ends_within_the_idle_window(page, registry, sessions):
    """Closing the console (here: the browser tab goes elsewhere) stops the keepalives, and
    Central ends the session within its idle window: the Display goes back to the saved values
    by itself."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        assert sessions.state_now() == "active"
        page.goto("about:blank")
        after = len(sessions.sent)
        page.wait_for_timeout(IDLE_SECONDS * 2 * 1000)
        assert sessions.sent[after:] == []
        assert sessions.state_now() in ("expired", "ended")


def test_done_and_revert_never_flicker_while_the_session_is_kept(page, registry, sessions):
    """The loop's own requests (keepalive twice a second) never disable Done or Revert: only
    the operator's Done or Revert does."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        _brighten(page)
        done = bar.get_by_role("button", name="Done", exact=True)
        expect(done).to_be_enabled()
        changes = page.evaluate("""() => new Promise((resolve) => {
            const bar = document.querySelector('[role="group"][aria-label="Show on the Display"]');
            const buttons = [...bar.querySelectorAll("button")];
            let count = 0;
            const watch = new MutationObserver((records) => { count += records.length; });
            buttons.forEach((button) => watch.observe(button, { attributes: true, attributeFilter: ["disabled"] }));
            setTimeout(() => { watch.disconnect(); resolve(count); }, 2000);
        })""")
        assert changes == 0
        assert sessions.count("keepalive") >= 3


def test_a_passing_refusal_keeps_the_session_and_clears_itself(page, registry, sessions):
    """A refusal that passes (the Pi was briefly away) is shown while the page keeps the session
    and keeps trying, and goes away once Central answers again: the session never lapses."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        frame = open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        sessions.fail["keepalive"] = (409, "trial_display_unavailable")
        expect(frame.get_by_role("alert")).to_contain_text("This Frame's Pi is not connected to Photo Wall")
        expect(frame.get_by_role("alert")).to_have_count(0)
        page.wait_for_timeout(IDLE_SECONDS * 2 * 1000)
        assert sessions.count("begin") == 1, sessions.sent
        assert sessions.state_now() == "active"


def test_leaving_the_tab_with_changes_asks_keep_or_revert(page, registry, sessions):
    """LeaveGuard: another tab with changes not kept asks first. Stay stays; Revert undoes
    them and goes; Keep saves them (once the Pi presents them) and goes."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "picture")
        tabs = page.get_by_role("tablist", name="Frame settings", exact=True)
        _brighten(page)
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)

        tabs.get_by_role("tab", name="Overview", exact=True).click()
        dialog = page.get_by_role("dialog", name="Keep your changes?")
        dialog.get_by_role("button", name="Stay", exact=True).click()
        expect(dialog).to_have_count(0)
        assert page.evaluate("location.hash").endswith("/picture")

        tabs.get_by_role("tab", name="Overview", exact=True).click()
        dialog.get_by_role("button", name="Revert", exact=True).click()
        expect(page.get_by_role("region", name="Now", exact=True)).to_be_visible()
        assert _saved(registry).gain == GAIN

        tabs.get_by_role("tab", name="Picture", exact=True).click()
        expect(_brighten(page, 2)).to_have_value("160")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        tabs.get_by_role("tab", name="Hardware", exact=True).click()
        dialog.get_by_role("button", name="Keep", exact=True).click()
        expect(page.get_by_role("region", name="Frame profile", exact=True)).to_be_visible()
        assert _saved(registry).gain == 1.6


def test_leaving_the_page_with_changes_says_they_were_reverted_on_return(page, registry, sessions):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "picture")
        _brighten(page)
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        visit(page, "#/wall")
        expect(page.get_by_role("button", name=f"Frame {FRAME}", exact=True)).to_be_visible()
        open_frame(page, FRAME, "picture")
        expect(page.get_by_role("status").filter(has_text="Your unsaved changes were reverted at")).to_have_text(
            re.compile(r"reverted at \d\d:\d\d:\d\d because you left this Frame's page\.$"))
        expect(page.get_by_role("slider", name=BRIGHTNESS, exact=True)).to_have_value("150")
        assert _saved(registry).gain == GAIN


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_position_tab_fits_a_phone(page, registry, sessions, scheme):
    """At a phone's width the Position tab scrolls only up and down, its bar stays in reach."""
    _seed(registry)
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme=scheme)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "position")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        expect(_bar(page).get_by_role("button", name="Done", exact=True)).to_be_in_viewport()


@pytest.mark.parametrize("how", ["revert", "tab"])
def test_a_revert_or_a_quick_return_never_races_its_own_end(page, registry, sessions, how):
    """Revert (or leaving the tab and coming straight back) ends the session; the next session
    is begun only once that end is answered, so the page never reads its own session as someone
    else's. The fake holds the `end` unanswered for a while, as a slow network would.

    Mutation: begin without waiting for this page's own end -> Central refuses the begin
    (`trial_already_active`) and the page says someone else is adjusting -> RED."""
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        tabs = page.get_by_role("tablist", name="Frame settings", exact=True)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        sessions.hold_end = True
        if how == "revert":
            _brighten(page)
            expect(bar.get_by_role("status")).to_have_text(PRESENTED)
            bar.get_by_role("button", name="Revert", exact=True).click()
        else:
            tabs.get_by_role("tab", name="Overview", exact=True).click()
            tabs.get_by_role("tab", name="Picture", exact=True).click()
            bar = _bar(page)
        page.wait_for_timeout(1500)  # long enough for a begin that did not wait
        assert sessions.refused_busy == 0, sessions.sent
        sessions.release()
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(page.get_by_text("Someone else is adjusting this Frame")).to_have_count(0)
        assert sessions.refused_busy == 0, sessions.sent
        assert sessions.state_now() == "active"


def test_the_side_rail_stays_put_and_the_tabs_never_wrap(page, registry, sessions):
    """Desktop: the section rail sits at the same height on every tab, however tall the tab.
    Phone: the tabs are one row that scrolls sideways."""
    _seed(registry)
    page.set_viewport_size({"width": 1440, "height": 900})
    with operator_server(registry.db, registry.clock) as origin:
        sign_in(page, origin)
        rail = page.get_by_role("navigation", name="Sections", exact=True)
        tops = {}
        for tab in ("overview", "position", "picture", "hardware"):
            open_frame(page, FRAME, tab)
            if tab in ("position", "picture"):
                expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
            tops[tab] = rail.bounding_box()["y"]
        assert len(set(tops.values())) == 1, tops

        page.set_viewport_size({"width": 320, "height": 700})
        tabs = page.get_by_role("tablist", name="Frame settings", exact=True)
        rows = {round(tabs.get_by_role("tab", name=name, exact=True).bounding_box()["y"])
                for name in ("Overview", "Position", "Picture", "Hardware")}
        assert len(rows) == 1, rows
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
