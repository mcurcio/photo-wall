"""The Frame page's Picture tab: brightness, contrast, colour temperature and gamma of the display
that feeds the Frame, live, with Done, Revert and Copy (roadmap 1c, slice K1; design-language §8 row
B5, the Live model; console design §5 Picture, settings #111-#114, #119).

Every write goes through the operator API the tab calls, on the real Registry and display store.
The Pi is a fake that answers like one (`FakePi`): it reads the Output document the worker's own
source projects and records, and reports for its picture change through the link store's commit and
its judge (`test_display_picture_settings._probe`), so the tab's words and Done come from Central's
real status, never from the console alone.

Mutation probes: enable Done before the phase is `shown` -> the Done case fails; send every drag
event -> the rate case fails; Copy sending where a setting acts or the fit -> the Copy case fails.
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_display_identity import NO_SERIAL, _bind, _frame, _sql, enroll_pi
from test_display_picture_settings import _probe, _projected

from contracts.node_output import ActsOn, WriteResult

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "kitchen"
SERIAL = "pi-picture"
PRESENTED = "Presented by the Pi"
WAITING = "Previewing — waiting for the Pi"


def _off_thread(call, *args, **kwargs):
    """A recording helper off Playwright's thread, whose event loop is running."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call, *args, **kwargs).result()


class FakePi:
    """The Pi on one Output: every POLL_S it takes the document the worker's source projects now and
    reports for its picture change, brightness on the display read back as written (`taken`).
    `presenting` False holds the Player's `presented` echo back, so the latest change is not shown."""

    POLL_S = 0.15

    def __init__(self, registry, serial=SERIAL, output_id="HDMI-A-1"):
        self.registry, self.serial, self.output_id = registry, serial, output_id
        self.presenting = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def answer(self):
        document = _projected(self.registry, self.serial, self.output_id)
        brightness = document.picture.brightness
        read = brightness.value if (brightness.acts_on is ActsOn.DISPLAY and isinstance(brightness.value, int)) else None
        _probe(self.registry, brightness=True, contrast=False, for_change=document.picture_change, read=read,
               presented=document.picture_change if self.presenting else None,
               result=WriteResult.TAKEN if read is not None else None,
               serial=self.serial, output_id=self.output_id, identity=NO_SERIAL)

    def _run(self):
        while not self._stop.is_set():
            self.answer()
            self._stop.wait(self.POLL_S)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def _seen(registry, frame=FRAME, serial=SERIAL, output_id="HDMI-A-1", player=None):
    """A bound Frame whose Pi reported a display that answered the brightness check (so brightness
    acts on the display and contrast as picture adjustment, placed at the first probe)."""
    player = player or enroll_pi(registry, serial)
    _bind(registry, _frame(registry, frame), player, output_id)
    _off_thread(_probe, registry, brightness=True, contrast=False, serial=serial, output_id=output_id,
                identity=NO_SERIAL)
    return player


def _display(registry, frame=FRAME):
    [row] = _sql(registry, "SELECT d.* FROM frames f JOIN bindings b ON b.frame_id=f.id "
                           "JOIN output_displays o ON o.player_id=b.player_id AND o.output_id=b.output_id "
                           "JOIN displays d ON d.id=o.display_id WHERE f.id=%s", (frame,))
    return row


def _store(registry, frame=FRAME, **values):
    display_id = _display(registry, frame)["id"]
    sets = ", ".join(f"{column}=%s" for column in values)
    _sql(registry, f"UPDATE displays SET {sets} WHERE id=%s", (*values.values(), display_id))


def _picture(page):
    return page.get_by_role("region", name="Picture", exact=True)


def _bar(page):
    return page.get_by_role("group", name="Show on the Display", exact=True)


def _row(page, label):
    """A setting's Field: its label, control and where-it-acts words."""
    return _picture(page).locator("div", has=page.get_by_role("slider", name=label, exact=True)).last


def _press(page, label, key, times=1, pause_ms=0):
    """Press `key` on a slider `times` times, `pause_ms` apart (a drag's pace)."""
    slider = page.get_by_role("slider", name=label, exact=True)
    slider.focus()
    for _ in range(times):
        page.keyboard.press(key)
        if pause_ms:
            page.wait_for_timeout(pause_ms)
    return slider


def _edits(page):
    """Every edit the page posts: (monotonic time, body)."""
    sent = []
    page.on("request", lambda request: sent.append((time.monotonic(), request.post_data_json))
            if request.url.endswith("/edits") and request.method == "POST" else None)
    return sent


def test_each_slider_says_where_it_acts(page, registry):
    """Brightness answered the Pi's check, so it acts on the display; contrast did not, so it is a
    Photo Wall picture adjustment. Nobody set either: as the display has it, nothing written."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, "Brightness")).to_contain_text("As the display has it")
        expect(_row(page, "Contrast")).to_contain_text("As the display has it")

        _press(page, "Brightness", "ArrowRight")
        expect(_row(page, "Brightness")).to_contain_text(re.compile(r"On the display · \d+ %"))
        _press(page, "Contrast", "ArrowLeft")
        expect(_row(page, "Contrast")).to_contain_text(re.compile(r"Photo Wall picture adjustment · \d+ %"))
        expect(_row(page, "Colour temperature")).to_contain_text("Photo Wall picture adjustment · 6500 K")
        expect(_row(page, "Gamma")).to_contain_text("Photo Wall picture adjustment · 2.2")
        expect(_picture(page)).not_to_contain_text(re.compile(r"Panel|Actuator|Display Host|calibration|\bgain\b", re.I))


def test_dragging_sends_at_most_two_edits_a_second_and_the_last_where_it_stops(page, registry):
    """Twenty steps of the slider over about two seconds: the display is written at most twice a
    second, and the value where the slider stopped is the last one sent.

    Mutation: send every change at once (EDIT_GAP_MS 0) -> about twenty edits, closer than 0.5 s."""
    _seen(registry)
    _store(registry, brightness=20)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        edits = _edits(page)
        started = time.monotonic()
        slider = _press(page, "Brightness", "ArrowRight", 20, pause_ms=100)
        took = time.monotonic() - started
        expect(slider).to_have_value("40")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        times = [at for at, _body in edits]
        assert edits and edits[-1][1]["value"] == 40, edits
        assert len(edits) <= 2 * took + 2, (len(edits), took)
        assert all(later - earlier >= 0.45 for earlier, later in zip(times, times[1:])), times


def test_done_waits_until_the_pi_shows_the_latest_change_then_keeps_it(page, registry):
    """Mutation: offer Done before `shown` -> Done is enabled while the Pi holds its echo back."""
    _seen(registry)
    _store(registry, brightness=50)
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        done = bar.get_by_role("button", name="Done", exact=True)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(done).to_be_disabled()  # nothing changed

        pi.presenting = False
        _press(page, "Brightness", "ArrowRight", 3)
        expect(bar.get_by_role("status")).to_have_text(WAITING)
        page.wait_for_timeout(1500)
        expect(done).to_be_disabled()
        assert _display(registry)["brightness"] == 50  # not kept yet

        pi.presenting = True
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(done).to_be_enabled()
        done.click()
        expect(_picture(page).get_by_role("status").filter(has_text="Saved at")).to_have_text(
            re.compile(r"^Saved at \d\d:\d\d:\d\d\.$"))
        assert _display(registry)["brightness"] == 53
        expect(page.get_by_role("slider", name="Brightness", exact=True)).to_have_value("53")
        expect(_row(page, "Brightness")).to_contain_text("On the display · 53 %")


def test_revert_puts_back_the_kept_value(page, registry):
    _seen(registry)
    _store(registry, brightness=40)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        slider = _press(page, "Brightness", "ArrowRight", 5)
        expect(slider).to_have_value("45")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Revert", exact=True).click()
        expect(slider).to_have_value("40")
        expect(bar.get_by_role("button", name="Revert", exact=True)).to_be_disabled()
        assert _display(registry)["brightness"] == 40
        expect(_row(page, "Brightness")).to_contain_text("On the display · 40 %")


def test_moving_brightness_to_picture_adjustment_changes_its_label(page, registry):
    _seen(registry)
    _store(registry, brightness=60)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(_row(page, "Brightness")).to_contain_text("On the display · 60 %")
        _picture(page).get_by_role("button", name="Use picture adjustment for brightness", exact=True).click()
        expect(_row(page, "Brightness")).to_contain_text("Photo Wall picture adjustment · 60 %")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(_picture(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _display(registry)["brightness_acts_on"] == "picture-adjustment"
        # The display answered its check, so brightness can go back onto it.
        expect(_picture(page).get_by_role("button", name="Use the display's own brightness", exact=True)).to_be_visible()


def _positions(registry):
    return {frame.id: (frame.calibration.model_dump(mode="json"), frame.readiness)
            for frame in registry.inventory().frames}


def test_copy_to_two_frames_copies_the_values_and_never_where_they_act_or_position(page, registry):
    """Copy (#119): the four values of this Frame's display go to two other Frames' displays; where
    each setting acts there and both Frames' position (fit, corners, readiness) stay as they were.

    Mutation: copy where each setting acts too -> hall's brightness stays on the display is RED."""
    player = _seen(registry)
    _seen(registry, "hall", output_id="HDMI-A-2", player=player)
    _seen(registry, "stairs", serial="pi-stairs")
    _store(registry, brightness=70, contrast=30, colour_temperature_kelvin=5000, gamma_tenths=24)
    _store(registry, "hall", brightness_acts_on="picture-adjustment")
    before = _positions(registry)
    sent = []
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.on("request", lambda request: sent.append(request.post_data_json)
                if request.url.endswith("/picture-copies") else None)
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        picture = _picture(page)
        picture.get_by_role("button", name="Copy to other Frames", exact=True).click()
        picture.get_by_role("switch", name="Frame hall", exact=True).click()
        picture.get_by_role("switch", name="Frame stairs", exact=True).click()
        picture.get_by_role("button", name="Copy to 2 Frames", exact=True).click()
        results = picture.get_by_role("group", name="Copy results", exact=True)
        expect(results).to_contain_text("Frame hall: Copied")
        expect(results).to_contain_text("Frame stairs: Copied")

    assert sent == [{"to_frames": ["hall", "stairs"]}]
    for frame, acts_on in (("hall", "picture-adjustment"), ("stairs", "display")):
        display = _display(registry, frame)
        assert (display["brightness"], display["contrast"], display["colour_temperature_kelvin"],
                display["gamma_tenths"]) == (70, 30, 5000, 24)
        assert display["brightness_acts_on"] == acts_on
    assert _positions(registry) == before


def test_an_unbound_frame_is_sent_to_its_hardware_tab(page, registry):
    _frame(registry, FRAME)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_picture(page).get_by_role("status")).to_have_text(
            "Choose which Pi and HDMI output feed this Frame first (Hardware tab).")

