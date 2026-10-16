"""The Frame page's Picture tab: the display's own settings and Photo Wall's, each on its own, live,
with Done, Revert and Copy (decision 0022, the picture split, slices S2 and S5; roadmap 1c's Live model,
design-language §8 row B5; console design §5 Picture, settings #111-#114, #119); display tuning's test
pattern (#101) and signal range, each with its own probe below.

Every write goes through the operator API the tab calls, on the real Registry and display store.
The Pi is a fake that answers like one (`FakePi`): it reads the picture document the worker's own
source projects and records, and reports for its picture change through the link store's commit and
its judge (`test_display_picture_settings._picture_report`), so the tab's words and Done come from
Central's real status, never from the console alone.

Mutation probes: enable Done before the phase is `shown` -> the Done case fails; send every drag
event -> the rate case fails; one edit route for both groups -> the two-groups case fails; render a
READ_ONLY_CHOICE row with a select -> the list-settings case's "no control" assertion fails; drop
FrameInput from the Picture tab (pages/frame-page.tsx) -> the input case fails. The Display's light
(decision 0028) carries its own probes below.
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from console_tasks import connect, open_frame
from operator_harness import operator_server
from playwright.sync_api import expect
from test_display_identity import NO_SERIAL, _bind, _frame, _report, _sql, enroll_pi
from test_display_picture_settings import OFFERED, _picture, _picture_report

from contracts.node_picture import (
    Choice,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    RangeKind,
    SignalRange,
    SignalReport,
    SignalResult,
    WriteResult,
)

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
AS_IT_HAS_IT = "As the display has it"
# Brightness a level of 100; colour preset a list of two named choices; display mode answers a value
# but lists none (read-only).
LISTS = (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 60),
         HardwareSetting("14", HardwareRange(RangeKind.CHOICE, None, (Choice(4, "5000 K"), Choice(5, "6500 K"))), 4),
         HardwareSetting("DC", HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ()), 2))


def _off_thread(call, *args, **kwargs):
    """A recording helper off Playwright's thread, whose event loop is running."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call, *args, **kwargs).result()


class FakePi:
    """The Pi on one Output: every POLL_S it takes the picture document the worker's source projects
    now and reports for its picture change, each hardware value written and read back as written
    (`taken`), and the display's list re-read with those values current (the periodic re-read).
    `presenting` False holds the Player's `presented` echo back, so the latest change is not shown;
    `buttons` (code: value) is what someone set on the display's own buttons since, which the re-read
    finds.

    The signal range: the connector starts at Auto; while `signalling` the Pi carries out each
    document's range with `signal_result` (applied: the connector takes it; failed: it keeps its
    range; unsupported: Auto, nothing set) and answers for that change, else it repeats its last
    answer, for an older change. The test pattern: the Player presents the document's ask while
    `presenting`, until `bound_over` says the Pi took it off at its bound."""

    POLL_S = 0.15

    def __init__(self, registry, serial=SERIAL, output_id="HDMI-A-1", offered=OFFERED):
        self.registry, self.serial, self.output_id, self.offered = registry, serial, output_id, offered
        self.presenting = True
        self.buttons = {}
        self.signalling = True
        self.signal_result = SignalResult.APPLIED
        self.bound_over = False
        self._connector = SignalRange.AUTO
        self._signal = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def answer(self):
        document = _picture(self.registry, self.serial, self.output_id)
        results = {code: HardwareResult(WriteResult.TAKEN, 60, value) for code, value in document.hardware.items()}
        read = {**document.hardware, **self.buttons}
        offered = tuple(replace(setting, current=read.get(setting.code, setting.current)) for setting in self.offered)
        if self.signalling:
            if self.signal_result is SignalResult.APPLIED:
                self._connector = document.signal_range
            in_force = SignalRange.AUTO if self.signal_result is SignalResult.UNSUPPORTED else self._connector
            self._signal = SignalReport(document.picture_change, in_force, self.signal_result)
        shown = document.test_pattern if self.presenting and not self.bound_over else None
        _picture_report(self.registry, offered=offered, for_change=document.picture_change, results=results,
                        presented=document.picture_change if self.presenting else None,
                        serial=self.serial, output_id=self.output_id, signal=self._signal, test_pattern_shown=shown)

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


def _seen(registry, frame=FRAME, serial=SERIAL, output_id="HDMI-A-1", player=None, offered=OFFERED):
    """A bound Frame whose Pi reported a display offering `offered` (brightness and contrast as
    levels of 100); `offered` None: the Pi has sent no picture report yet (the list is not known)."""
    player = player or enroll_pi(registry, serial)
    _bind(registry, _frame(registry, frame), player, output_id)
    _off_thread(_report, registry, serial, output_id, NO_SERIAL)
    if offered is not None:
        _off_thread(_picture_report, registry, serial=serial, output_id=output_id, offered=offered)
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
    and Photo Wall's contrast sends one edit each, to its own route, and both reach the documents."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("As the display has it")
        expect(_row(page, DISPLAY, "Contrast")).to_contain_text("As the display has it")
        expect(_row(page, PHOTO_WALL, "Contrast")).to_contain_text("100 % changes nothing")
        expect(_slider(page, PHOTO_WALL, "Contrast")).to_have_attribute("aria-valuetext", "100 %")
        for label in ("Contrast", "Colour temperature", "Gamma"):
            expect(_slider(page, PHOTO_WALL, label)).to_be_visible()
        expect(_picture_tab(page)).not_to_contain_text(re.compile(
            r"On the display|Photo Wall picture adjustment|Changed on the display's buttons|Panel|calibration|\bgain\b",
            re.I))

        hardware, software = _edits(page, "hardware-edits"), _edits(page, "software-edits")
        _press(page, DISPLAY, "Brightness", "ArrowRight")
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("61 % (61 of 100)")
        _press(page, PHOTO_WALL, "Contrast", "ArrowRight")
        expect(_row(page, PHOTO_WALL, "Contrast")).to_contain_text("101 %")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        assert [body["code"] for _at, body in hardware] == ["10"], hardware
        assert [(body["setting"], body["value"]) for _at, body in software] == [("contrast", 101)], software
        document = _off_thread(_picture, registry)
        assert (set(document.hardware), document.software.contrast_percent) == ({"10"}, 101)


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
        _press(page, PHOTO_WALL, "Contrast", "ArrowRight", 14)
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
        assert (stored["hardware_picture"], stored["software_contrast_percent"]) == ({"10": 53}, 114)
        expect(_slider(page, DISPLAY, "Brightness")).to_have_value("53")
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("53 % (53 of 100)")


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
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("60 % (60 of 100)")
        _group(page, DISPLAY).get_by_role("button", name="As the display has it: brightness", exact=True).click()
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("As the display has it")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _display(registry)["hardware_picture"] == {}


def test_list_settings_a_picker_of_exactly_their_choices_and_a_read_only_line_with_no_control(page, registry):
    """A display offering brightness (a level of 100), colour preset (choices 4 "5000 K" and 5
    "6500 K") and display mode (read-only): a slider, a select of exactly the two choices plus "As
    the display has it", and a line with no control. Choosing "6500 K" sends {code 14, value 5};
    "As the display has it" sends value None.

    Mutation: render a READ_ONLY_CHOICE row with a select -> the "no control" assertion fails."""
    _seen(registry, offered=LISTS)
    with FakePi(registry, offered=LISTS), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        display = _group(page, DISPLAY)
        expect(_slider(page, DISPLAY, "Brightness")).to_be_visible()
        preset = display.get_by_role("combobox", name="Select color preset", exact=True)
        expect(preset.locator("option")).to_have_text([AS_IT_HAS_IT, "5000 K", "6500 K"])
        expect(preset).to_have_value("as-the-display-has-it")
        expect(preset).to_have_accessible_description(AS_IT_HAS_IT)
        expect(display.locator("p", has_text="Display mode")).to_have_text("Display mode: 2")
        expect(display.get_by_role("combobox")).to_have_count(1)
        expect(display.get_by_role("slider")).to_have_count(1)
        expect(display.get_by_role("button")).to_have_count(0)

        edits = _edits(page, "hardware-edits")
        preset.select_option(label="6500 K")
        expect(preset).to_have_value("5")
        expect(preset).to_have_accessible_description("6500 K")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        assert _off_thread(_picture, registry).hardware == {"14": 5}
        page.wait_for_timeout(600)
        preset.select_option(label=AS_IT_HAS_IT)
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        assert [(body["code"], body["value"]) for _at, body in edits] == [("14", 5), ("14", None)], edits


def test_a_value_changed_on_the_displays_buttons_is_shown_not_fought(page, registry):
    """Owner addition (2026-10-12): the Pi re-reads the set codes; a current value other than the one
    set in Photo Wall says "Changed on the display's buttons", and nothing is written back."""
    _seen(registry)
    _store(registry, hardware_picture='{"10": 40}')
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("40 % (40 of 100)")
        edits = _edits(page, "hardware-edits")
        pi.buttons = {"10": 35}
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("Changed on the display's buttons: 35 % (35 of 100)")
        page.wait_for_timeout(600)
        assert edits == []
        assert _display(registry)["hardware_picture"] == {"10": 40}


REMOVED = ("Brightness", "Saturation", "Soft highlights", "Black floor", "LUT amount")


def _typed(page, label):
    """A typed value of the Photo Wall group (Matched light, Display black)."""
    return _group(page, PHOTO_WALL).get_by_role("spinbutton", name=label, exact=True)


def test_the_removed_sliders_are_gone_and_matched_light_is_edited_live(page, registry):
    """Decision 0028 (owner, 2026-10-15, Q2 B: "the paper replaces them"): the Photo Wall group has no
    brightness, saturation, soft highlights, black floor or LUT; Matched light typed as 600 lx is sent
    to light-edits with the Display black in force, reaches the Pi's picture document live, is shown,
    and Done keeps it; the line says White is the TV's own white for the paper match.

    Mutation: send `matched` instead of `matched_lux` -> Central's 422, the document keeps 1,000 lx."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        group = _group(page, PHOTO_WALL)
        for label in REMOVED:
            expect(group.get_by_role("slider", name=label, exact=True)).to_have_count(0)
        expect(group.get_by_role("combobox", name="LUT", exact=True)).to_have_count(0)
        expect(group).to_contain_text("The White pattern is the TV's own white")
        before = _off_thread(_picture, registry).light
        expect(_typed(page, "Matched light")).to_have_value(f"{before.matched_lux:g}")

        edits = _edits(page, "light-edits")
        _typed(page, "Matched light").fill("600")
        expect(bar.get_by_role("button", name="Done", exact=True)).to_be_enabled()
        assert _off_thread(_picture, registry).light.matched_lux == 600
        page.wait_for_timeout(600)
        bodies = [body for _at, body in edits]
        assert bodies and {key: bodies[-1][key] for key in ("matched_lux", "black")} == {
            "matched_lux": 600, "black": before.black}, bodies
        assert set(bodies[-1]) == {"matched_lux", "black", "expected_sequence"}, bodies
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()

        page.reload()
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_typed(page, "Matched light")).to_have_value("600")


def test_display_black_is_typed_as_a_percent_and_sent_as_a_share_and_revert_puts_it_back(page, registry):
    """Display black 0.05 % of white is sent as 0.0005 with the matched lux in force; a value outside
    the range is said in the field and never sent; Revert puts the kept light back.

    Mutation: send the percent as typed (0.05) -> the share assertion fails."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        kept = _off_thread(_picture, registry).light
        edits = _edits(page, "light-edits")
        black = _typed(page, "Display black")
        black.fill("9")
        expect(_group(page, PHOTO_WALL)).to_contain_text("From 0.001 to 5 %")
        black.fill("0.05")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        assert [(body["matched_lux"], body["black"]) for _at, body in edits] == [(kept.matched_lux, 0.0005)], edits
        assert _off_thread(_picture, registry).light.black == 0.0005
        bar.get_by_role("button", name="Revert", exact=True).click()
        expect(black).to_have_value(f"{kept.black * 100:g}")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        assert _off_thread(_picture, registry).light == kept


def _positions(registry):
    return {frame.id: (frame.calibration.model_dump(mode="json"), frame.readiness)
            for frame in registry.inventory().frames}


def test_copy_to_two_frames_copies_photo_walls_values_and_never_position(page, registry):
    """Copy (#119): Photo Wall's three of this Frame's display go to two other Frames' displays, and
    each display setting set here where the target offers it the same way (hall: brightness, a level
    of 100 on both); stairs' Pi has not said what its display offers, so brightness is skipped there
    and named. Both Frames' position (fit, corners, readiness) stays as it was."""
    player = _seen(registry)
    _seen(registry, "hall", output_id="HDMI-A-2", player=player)
    _seen(registry, "stairs", serial="pi-stairs", offered=None)
    _store(registry, software_contrast_percent=30, colour_temperature_kelvin=5000,
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
        expect(results.locator("p", has_text="Frame hall")).to_have_text("Frame hall: Copied")
        expect(results.locator("p", has_text="Frame stairs")).to_have_text("Frame stairs: Copied; skipped: Brightness")

    assert sent == [{"to_frames": ["hall", "stairs"]}]
    for frame, hardware in (("hall", {"10": 70, "12": 10}), ("stairs", {})):
        display = _display(registry, frame)
        assert (display["software_contrast_percent"], display["colour_temperature_kelvin"],
                display["gamma_tenths"]) == (30, 5000, 24)
        assert display["hardware_picture"] == hardware
    assert _positions(registry) == before


INPUT = "Switch the display to this input on power-on"


def test_the_input_setting_is_on_both_tabs_and_a_change_on_picture_shows_on_power(page, registry):
    """Decision 0022 R10 (owner, 2026-10-12: "Let’s put it on both. That seems like the optimal
    UX"): the display's input setting (#117) is on the Picture tab as well as the Power tab, one
    stored setting: turned off on Picture, it is saved by itself (outside Done and Revert) and the
    Power tab shows it off."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        on_picture = page.get_by_role("region", name="Input", exact=True).get_by_role("switch", name=INPUT)
        expect(on_picture).to_be_checked()
        with page.expect_request(lambda request: "/power-settings" in request.url
                                 and request.method == "PUT") as captured:
            on_picture.click()
        assert captured.value.post_data_json["switch_input_on_power_on"] is False
        expect(page.locator(f'[data-setting="{INPUT}"]')).to_contain_text("Saved")
        expect(_bar(page).get_by_role("button", name="Done", exact=True)).to_be_disabled()  # nothing to keep
        assert _display(registry)["switch_input_on_power_on"] is False

        power = open_frame(page, FRAME, "power").get_by_role("region", name="Power", exact=True)
        expect(power.get_by_role("switch", name=INPUT)).not_to_be_checked()
        expect(power.get_by_role("button", name=f"Reset {INPUT} (Default: On)")).to_be_visible()


def test_an_unbound_frame_is_sent_to_its_hardware_tab(page, registry):
    _frame(registry, FRAME)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_picture_tab(page).get_by_role("status")).to_have_text(
            "Choose which Pi and HDMI output feed this Frame first (Hardware tab).")


TEST_PATTERN = "Test pattern"
SIGNAL = "Signal range"
RESTARTING = "Waiting for the Pi: a change restarts the picture on every display of this Pi."
SHOWN = "The Pi reports it shown on every Frame of this display."
PAPER = ("Tape a sheet of white paper beside the Frame and match its white, then type the lux the meter reads as "
         "Matched light.")
READING = ("Switch Signal range between Full and Limited. Black darker on Full with every step visible: keep Full. "
           "Steps vanish into black on Full: choose Limited. No change: the display follows the Pi, the glow is its "
           "backlight; keep Full, it keeps all 256 steps.")


def _choice(page, group, name):
    return _group(page, group).get_by_role("radio", name=name, exact=True)


def _shown_pattern(page):
    """The Test pattern's picker (a Select: six choices)."""
    return _group(page, TEST_PATTERN).get_by_role("combobox", name="Show", exact=True)


def _show(page, label):
    _shown_pattern(page).select_option(label=label)


def _sequence(registry):
    [row] = _sql(registry, "SELECT sequence FROM picture_sessions")
    return row["sequence"]


def test_full_says_the_restart_until_the_pi_answers_and_done_waits_for_that_answer(page, registry):
    """Acceptance 1 and 2: choosing Full sends exactly one signal-range-edit with the session's
    sequence; while the Pi presents the change but has not answered its signal, the group says it
    waits for the Pi, whose change restarts every display's picture, and Done stays disabled; once it answers, "The Pi sends Full." and
    Done keeps Full.

    Mutations: send "Full" capitalised -> Central's 422, nothing in the document; let the session
    take the Player's echo alone as shown -> Done is enabled while the signal is unanswered."""
    _seen(registry)
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        done = bar.get_by_role("button", name="Done", exact=True)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(_choice(page, SIGNAL, "Auto")).to_be_checked()
        expect(_group(page, SIGNAL)).to_contain_text("The Pi chooses: Limited for TV modes.")
        edits = _edits(page, "signal-range-edits")
        sequence = _off_thread(_sequence, registry)

        pi.signalling = False
        _choice(page, SIGNAL, "Full").click()
        expect(_group(page, SIGNAL)).to_contain_text(RESTARTING)
        page.wait_for_timeout(1500)
        expect(done).to_be_disabled()
        assert [body for _at, body in edits] == [{"signal_range": "full", "expected_sequence": sequence}], edits
        assert _off_thread(_picture, registry).signal_range is SignalRange.FULL
        assert _display(registry)["signal_range"] == "auto"  # not kept yet

        pi.signalling = True
        expect(_group(page, SIGNAL)).to_contain_text("The Pi sends Full.")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(done).to_be_enabled()
        done.click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _display(registry)["signal_range"] == "full"
        expect(_choice(page, SIGNAL, "Full")).to_be_checked()


def test_revert_puts_the_stored_signal_range_back(page, registry):
    """Acceptance 2: Revert after Full shows Auto again, the stored range, and the document asks for Auto."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        _choice(page, SIGNAL, "Full").click()
        expect(_group(page, SIGNAL)).to_contain_text("The Pi sends Full.")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Revert", exact=True).click()
        expect(_choice(page, SIGNAL, "Auto")).to_be_checked()
        expect(bar.get_by_role("button", name="Revert", exact=True)).to_be_disabled()
        assert _display(registry)["signal_range"] == "auto"
        for _ in range(50):
            if _off_thread(_picture, registry).signal_range is SignalRange.AUTO:
                break
            page.wait_for_timeout(100)
        else:
            raise AssertionError("Revert left the document asking for Full")


def test_white_shows_the_paper_line_and_off_sends_null(page, registry):
    """Acceptance 3: White sends {test_pattern: "white"}, the paper line shows, the Pi shows it on
    every Frame of the display; Off sends {test_pattern: null}, never "off".

    Mutation: send "off" for Off -> Central's 422, the pattern stays up and the body assertion fails."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        group = _group(page, TEST_PATTERN)
        expect(_shown_pattern(page)).to_have_value("off")
        expect(group).to_contain_text("Choose Off or leave this page; the Pi takes it off by itself after 10 min.")
        edits = _edits(page, "test-pattern-edits")
        _show(page, "White")
        expect(group).to_contain_text(PAPER)
        expect(group).to_contain_text(SHOWN)
        assert _off_thread(_picture, registry).test_pattern.pattern.value == "white"

        _show(page, "Off")
        expect(group).not_to_contain_text(PAPER)
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        assert [body["test_pattern"] for _at, body in edits] == ["white", None], edits
        assert _off_thread(_picture, registry).test_pattern is None


def test_black_level_says_how_to_read_it_and_once_ended_is_asked_for_again(page, registry):
    """Acceptance 4: the Black level shows the reading line; when the Pi takes it off at its bound
    the group says so (minutes from the view) and shows Off; choosing it again sends a new edit.

    Mutation: drop the reading line -> its text is not found."""
    _seen(registry)
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        group = _group(page, TEST_PATTERN)
        edits = _edits(page, "test-pattern-edits")
        _show(page, "Black level")
        expect(group).to_contain_text(READING)
        expect(group).to_contain_text(SHOWN)

        pi.bound_over = True
        expect(group).to_contain_text("Taken off after 10 min. Choose it again to show it.")
        expect(_shown_pattern(page)).to_have_value("off")
        first = _off_thread(_picture, registry).test_pattern.ask

        pi.bound_over = False
        _show(page, "Black level")
        expect(group).to_contain_text(SHOWN)
        expect(group).to_contain_text(READING)
        assert [body["test_pattern"] for _at, body in edits] == ["black-level", "black-level"], edits
        assert _off_thread(_picture, registry).test_pattern.ask > first


def _visibility(page, state):
    """The browser tab hidden or shown, as a phone switching to its camera does it."""
    page.evaluate("""(state) => {
        Object.defineProperty(document, "visibilityState", {configurable: true, get: () => state});
        document.dispatchEvent(new Event("visibilitychange"));
    }""", state)


def test_hiding_the_tab_keeps_a_shown_pattern_and_ends_a_session_without_one(page, registry):
    """Owner, 2026-10-14: "switching from my phone browser to my phone camera is enough to drop the
    white overlay". Hidden with White shown, the page sends no Revert and stops its keepalives, and
    the document keeps White past Central's 5 s window (a pattern's is a minute); shown again, the
    same session goes on. Hidden with no pattern, the page reverts at once.

    Mutation: revert on every hide (the effect without the pattern's exception) -> the DELETE is sent
    and the document drops White."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        group = _group(page, TEST_PATTERN)
        _show(page, "White")
        expect(group).to_contain_text(SHOWN)
        sent = []
        page.on("request", lambda request: sent.append((request.method, request.url.rsplit("/", 1)[-1]))
                if "/picture-sessions/" in request.url else None)
        [before] = _off_thread(_sql, registry, "SELECT id FROM picture_sessions")

        _visibility(page, "hidden")
        # A keepalive tick that fired before React took the hide in (its re-render clears the
        # interval) is not the page's answer to the hide: judge only what is sent once it settled.
        page.wait_for_timeout(300)
        assert not any(method == "DELETE" for method, _ in sent), sent
        sent.clear()
        page.wait_for_timeout(6500)
        assert not any(method == "DELETE" for method, _ in sent), sent
        assert not any(last == "keepalive" for _, last in sent), sent
        assert _off_thread(_picture, registry).test_pattern.pattern.value == "white"

        _visibility(page, "visible")
        expect(group).to_contain_text(SHOWN)
        page.wait_for_timeout(1200)
        assert any(last == "keepalive" for _, last in sent), sent
        assert _off_thread(_sql, registry, "SELECT id FROM picture_sessions") == [before]

        _show(page, "Off")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _visibility(page, "hidden")
        for _ in range(50):
            if any(method == "DELETE" for method, _ in sent):
                break
            page.wait_for_timeout(100)
        else:
            raise AssertionError(f"hidden with no pattern, the page never reverted: {sent}")


PATTERNS = ["Off", "White", "Grey ramp", "Black level", "Target: pre-software", "Target: post-software"]
PAPER_TARGET = {"Letter": "/v1/operator/calibration-target/paper.pdf?paper=letter",
                "A4": "/v1/operator/calibration-target/paper.pdf?paper=a4",
                "4 × 6 in photo": "/v1/operator/calibration-target/paper.pdf?paper=4x6"}


def _print_links(page):
    return _group(page, TEST_PATTERN).get_by_role("link")


def _await_asked(page, registry, wire):
    """Waits for Central's Output document to ask for the test pattern `wire` (None: none)."""
    for _ in range(50):
        ask = _off_thread(_picture, registry).test_pattern
        if (None if ask is None else ask.pattern.value) == wire:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the document never asked for {wire}")


def test_both_targets_are_offered_in_either_order_with_the_paper_targets_print_links(page, registry):
    """Calibration targets U1, acceptance 1 and 2 (owner R7, R8: either stage, any order; R1: the
    printable sheet): the picker lists six patterns; each target posts exactly its wire value, post
    before pre and back, with nothing gating either; the Pi shows each; with a target chosen the tab
    shows Letter, A4 and 4 × 6 in photo linking to the paper target and v2's three sentences (a piece of paper
    anywhere on the screen; no dark box, no uncut sheet), and none with another pattern or Off.

    Mutations: U1-wire, send "target-pre" -> Central's 422 and the body assertion fails; U1-links,
    render the links for every pattern -> the Off and White assertions fail; K1-words, restore v1's
    PAPER_UNDER_GLASS -> the "cut a piece" assertion fails."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        group = _group(page, TEST_PATTERN)
        expect(_shown_pattern(page).get_by_role("option")).to_have_text(PATTERNS)
        expect(_print_links(page)).to_have_count(0)
        edits = _edits(page, "test-pattern-edits")

        for label, wire in [("Target: post-software", "target-post-software"),
                            ("Target: pre-software", "target-pre-software"),
                            ("Target: post-software", "target-post-software")]:
            _show(page, label)
            expect(group).to_contain_text(SHOWN)
            # Design v2's three sentences: any size of paper, anywhere on the screen.
            expect(group).to_contain_text("cut a piece if the screen is small")
            expect(group).to_contain_text("ProRAW reads best")
            # Owner, 2026-10-14: switching to the phone's camera dropped the target; it now stays.
            expect(group).to_contain_text("the target stays up for a minute while this tab is hidden")
            expect(group).not_to_contain_text("Show a target")
            expect(group).not_to_contain_text("dark box")
            expect(group).not_to_contain_text("uncut")
            expect(_print_links(page)).to_have_text(list(PAPER_TARGET))
            for name, href in PAPER_TARGET.items():
                expect(group.get_by_role("link", name=name, exact=True)).to_have_attribute("href", href)
            _await_asked(page, registry, wire)

        # The page sends its draft, not each click (pictureSession.js, AT MOST TWO EDITS A SECOND):
        # Off chosen while White still waits behind the request in flight replaces it. So White
        # is let reach the document before Off, as an operator who looks at it would.
        _show(page, "White")
        expect(group).to_contain_text(PAPER)
        expect(_print_links(page)).to_have_count(0)
        _await_asked(page, registry, "white")
        _show(page, "Off")
        expect(_print_links(page)).to_have_count(0)
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        _await_asked(page, registry, None)
        assert [body["test_pattern"] for _at, body in edits] == [
            "target-post-software", "target-pre-software", "target-post-software", "white", None], edits


def test_a_failed_signal_says_what_the_display_still_gets_and_unsupported_says_so(page, registry):
    """Acceptance 5: a failed answer names the range the display still gets (the connector kept
    Auto: what the Pi chooses); an output with no range setting says the connection has none.

    Mutation: read signal.result "failed" as applied -> "The Pi chooses" alone, the assertion fails."""
    _seen(registry)
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        pi.signal_result = SignalResult.FAILED
        _choice(page, SIGNAL, "Full").click()
        expect(_group(page, SIGNAL)).to_contain_text("The Pi could not change it: the display still gets what the Pi chooses.")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)

        pi.signal_result = SignalResult.UNSUPPORTED
        _choice(page, SIGNAL, "Limited").click()
        expect(_group(page, SIGNAL)).to_contain_text("This display's connection has no signal range setting.")
