"""The Frame page's Picture tab: the display's own settings and Photo Wall's, each on its own, live,
with Done, Revert and Copy (decision 0022, the picture split, slice S2; roadmap 1c's Live model,
design-language §8 row B5; console design §5 Picture, settings #111-#114, #119).

Every write goes through the operator API the tab calls, on the real Registry and display store.
The Pi is a fake that answers like one (`FakePi`): it reads the picture document the worker's own
source projects and records, and reports for its picture change through the link store's commit and
its judge (`test_display_picture_settings._picture_report`), so the tab's words and Done come from
Central's real status, never from the console alone.

Mutation probes: enable Done before the phase is `shown` -> the Done case fails; send every drag
event -> the rate case fails; one edit route for both groups -> the two-groups case fails.
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
from test_display_identity import NO_SERIAL, _bind, _frame, _report, _sql, enroll_pi
from test_display_picture_settings import OFFERED, _picture, _picture_report

from contracts.node_picture import HardwareResult, WriteResult

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

FRAME = "kitchen"
SERIAL = "pi-picture"
PRESENTED = "Presented by the Pi"
WAITING = "Previewing — waiting for the Pi"
DISPLAY = "Display (hardware)"
PHOTO_WALL = "Photo Wall (software)"


def _off_thread(call, *args, **kwargs):
    """A recording helper off Playwright's thread, whose event loop is running."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call, *args, **kwargs).result()


class FakePi:
    """The Pi on one Output: every POLL_S it takes the picture document the worker's source projects
    now and reports for its picture change, each hardware value written and read back as written
    (`taken`). `presenting` False holds the Player's `presented` echo back, so the latest change is
    not shown."""

    POLL_S = 0.15

    def __init__(self, registry, serial=SERIAL, output_id="HDMI-A-1"):
        self.registry, self.serial, self.output_id = registry, serial, output_id
        self.presenting = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def answer(self):
        document = _picture(self.registry, self.serial, self.output_id)
        results = {code: HardwareResult(WriteResult.TAKEN, 60, value) for code, value in document.hardware.items()}
        _picture_report(self.registry, for_change=document.picture_change, results=results,
                        presented=document.picture_change if self.presenting else None,
                        serial=self.serial, output_id=self.output_id)

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
    """A bound Frame whose Pi reported a display offering brightness and contrast as levels of 100."""
    player = player or enroll_pi(registry, serial)
    _bind(registry, _frame(registry, frame), player, output_id)
    _off_thread(_report, registry, serial, output_id, NO_SERIAL)
    _off_thread(_picture_report, registry, serial=serial, output_id=output_id, offered=OFFERED)
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


def _picture_tab(page):
    return page.get_by_role("region", name="Picture", exact=True)


def _group(page, name):
    return _picture_tab(page).get_by_role("group", name=name, exact=True)


def _bar(page):
    return page.get_by_role("group", name="Show on the Display", exact=True)


def _slider(page, group, label):
    return _group(page, group).get_by_role("slider", name=label, exact=True)


def _row(page, group, label):
    """A setting's Field: its label, control and words."""
    return _group(page, group).locator("div", has=page.get_by_role("slider", name=label, exact=True)).last


def _press(page, group, label, key, times=1, pause_ms=0):
    """Press `key` on a slider `times` times, `pause_ms` apart (a drag's pace)."""
    slider = _slider(page, group, label)
    slider.focus()
    for _ in range(times):
        page.keyboard.press(key)
        if pause_ms:
            page.wait_for_timeout(pause_ms)
    return slider


def _edits(page, route):
    """Every edit the page posts to `route` ("hardware-edits" or "software-edits"): (monotonic time, body)."""
    sent = []
    page.on("request", lambda request: sent.append((time.monotonic(), request.post_data_json))
            if request.url.endswith("/" + route) and request.method == "POST" else None)
    return sent


def test_the_tab_shows_both_groups_and_each_slider_sends_its_own_edit(page, registry):
    """Acceptance 6: Display (hardware) and Photo Wall (software); moving the display's brightness
    and Photo Wall's brightness sends one edit each, to its own route, and both reach the documents."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("As the display has it")
        expect(_row(page, DISPLAY, "Contrast")).to_contain_text("As the display has it")
        expect(_row(page, PHOTO_WALL, "Brightness")).to_contain_text("100 % · 100 % changes nothing")
        for label in ("Contrast", "Colour temperature", "Gamma"):
            expect(_slider(page, PHOTO_WALL, label)).to_be_visible()
        expect(_picture_tab(page)).not_to_contain_text(re.compile(
            r"On the display|Photo Wall picture adjustment|Changed on the display's buttons|Panel|calibration|\bgain\b",
            re.I))

        hardware, software = _edits(page, "hardware-edits"), _edits(page, "software-edits")
        _press(page, DISPLAY, "Brightness", "ArrowRight")
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text(re.compile(r"Set in Photo Wall · \d+ of 100"))
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight")
        expect(_row(page, PHOTO_WALL, "Brightness")).to_contain_text("101 %")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        assert [body["code"] for _at, body in hardware] == ["10"], hardware
        assert [(body["setting"], body["value"]) for _at, body in software] == [("brightness", 101)], software
        document = _off_thread(_picture, registry)
        assert (set(document.hardware), document.software.brightness_percent) == ({"10"}, 101)


def test_dragging_sends_at_most_two_edits_a_second_and_the_last_where_it_stops(page, registry):
    """Twenty steps of the display's brightness over about two seconds: the display is written at
    most twice a second, and the value where the slider stopped is the last one sent.

    Mutation: send every change at once (EDIT_GAP_MS 0) -> about twenty edits, closer than 0.5 s."""
    _seen(registry)
    _store(registry, hardware_picture='{"10": 20}')
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        edits = _edits(page, "hardware-edits")
        started = time.monotonic()
        slider = _press(page, DISPLAY, "Brightness", "ArrowRight", 20, pause_ms=100)
        took = time.monotonic() - started
        expect(slider).to_have_value("40")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        times = [at for at, _body in edits]
        assert edits and edits[-1][1]["value"] == 40, edits
        assert len(edits) <= 2 * took + 2, (len(edits), took)
        assert all(later - earlier >= 0.45 for earlier, later in zip(times, times[1:])), times


def test_done_waits_until_the_pi_shows_the_latest_change_then_keeps_both_groups(page, registry):
    """Mutation: offer Done before `shown` -> Done is enabled while the Pi holds its echo back."""
    _seen(registry)
    _store(registry, hardware_picture='{"10": 50}')
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        done = bar.get_by_role("button", name="Done", exact=True)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(done).to_be_disabled()  # nothing changed

        pi.presenting = False
        _press(page, DISPLAY, "Brightness", "ArrowRight", 3)
        _press(page, PHOTO_WALL, "Brightness", "ArrowRight", 14)
        expect(bar.get_by_role("status")).to_have_text(WAITING)
        page.wait_for_timeout(1500)
        expect(done).to_be_disabled()
        assert _display(registry)["hardware_picture"] == {"10": 50}  # not kept yet

        pi.presenting = True
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(done).to_be_enabled()
        done.click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_have_text(
            re.compile(r"^Saved at \d\d:\d\d:\d\d\.$"))
        stored = _display(registry)
        assert (stored["hardware_picture"], stored["software_brightness_percent"]) == ({"10": 53}, 114)
        expect(_slider(page, DISPLAY, "Brightness")).to_have_value("53")
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("Set in Photo Wall · 53 of 100")


def test_revert_puts_back_the_kept_values(page, registry):
    _seen(registry)
    _store(registry, hardware_picture='{"10": 40}', software_contrast_percent=90)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        slider = _press(page, DISPLAY, "Brightness", "ArrowRight", 5)
        expect(slider).to_have_value("45")
        contrast = _press(page, PHOTO_WALL, "Contrast", "ArrowLeft", 5)
        expect(contrast).to_have_value("85")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Revert", exact=True).click()
        expect(slider).to_have_value("40")
        expect(contrast).to_have_value("90")
        expect(bar.get_by_role("button", name="Revert", exact=True)).to_be_disabled()
        stored = _display(registry)
        assert (stored["hardware_picture"], stored["software_contrast_percent"]) == ({"10": 40}, 90)


def test_as_the_display_has_it_hands_a_display_setting_back(page, registry):
    _seen(registry)
    _store(registry, hardware_picture='{"10": 60}')
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("Set in Photo Wall · 60 of 100")
        _group(page, DISPLAY).get_by_role("button", name="As the display has it: brightness", exact=True).click()
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("As the display has it")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _display(registry)["hardware_picture"] == {}


def _positions(registry):
    return {frame.id: (frame.calibration.model_dump(mode="json"), frame.readiness)
            for frame in registry.inventory().frames}


def test_copy_to_two_frames_copies_photo_walls_values_and_never_position(page, registry):
    """Copy (#119): Photo Wall's four of this Frame's display go to two other Frames' displays (S2
    copies no display setting); both Frames' position (fit, corners, readiness) stays as it was."""
    player = _seen(registry)
    _seen(registry, "hall", output_id="HDMI-A-2", player=player)
    _seen(registry, "stairs", serial="pi-stairs")
    _store(registry, software_brightness_percent=120, software_contrast_percent=30, colour_temperature_kelvin=5000,
           gamma_tenths=24, hardware_picture='{"10": 70}')
    _store(registry, "hall", hardware_picture='{"12": 10}')
    before = _positions(registry)
    sent = []
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        page.on("request", lambda request: sent.append(request.post_data_json)
                if request.url.endswith("/picture-copies") else None)
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        picture = _picture_tab(page)
        picture.get_by_role("button", name="Copy to other Frames", exact=True).click()
        picture.get_by_role("switch", name="Frame hall", exact=True).click()
        picture.get_by_role("switch", name="Frame stairs", exact=True).click()
        picture.get_by_role("button", name="Copy to 2 Frames", exact=True).click()
        results = picture.get_by_role("group", name="Copy results", exact=True)
        expect(results).to_contain_text("Frame hall: Copied")
        expect(results).to_contain_text("Frame stairs: Copied")

    assert sent == [{"to_frames": ["hall", "stairs"]}]
    for frame, hardware in (("hall", {"12": 10}), ("stairs", {})):
        display = _display(registry, frame)
        assert (display["software_brightness_percent"], display["software_contrast_percent"],
                display["colour_temperature_kelvin"], display["gamma_tenths"]) == (120, 30, 5000, 24)
        assert display["hardware_picture"] == hardware
    assert _positions(registry) == before


def test_an_unbound_frame_is_sent_to_its_hardware_tab(page, registry):
    _frame(registry, FRAME)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_picture_tab(page).get_by_role("status")).to_have_text(
            "Choose which Pi and HDMI output feed this Frame first (Hardware tab).")
