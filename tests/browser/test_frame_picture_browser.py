"""The Frame page's Picture tab: the display's own settings and Photo Wall's, each on its own, live,
with Done, Revert and Copy (decision 0022, the picture split, slices S2 and S5; roadmap 1c's Live model,
design-language §8 row B5; console design §5 Picture, settings #111-#114, #119).

Every write goes through the operator API the tab calls, on the real Registry and display store.
The Pi is a fake that answers like one (`FakePi`): it reads the picture document the worker's own
source projects and records, and reports for its picture change through the link store's commit and
its judge (`test_display_picture_settings._picture_report`), so the tab's words and Done come from
Central's real status, never from the console alone.

Mutation probes: enable Done before the phase is `shown` -> the Done case fails; send every drag
event -> the rate case fails; one edit route for both groups -> the two-groups case fails; render a
READ_ONLY_CHOICE row with a select -> the list-settings case's "no control" assertion fails; drop
FrameInput from the Picture tab (pages/frame-page.tsx) -> the input case fails. The print look's
rows (design R2, R4) carry their own probes below.
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
from test_print_look_luts import _as_printer, _monitor, _reduced_gamut

from central.infra.lut_store import PgLuts
from contracts.node_picture import (
    Choice,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    RangeKind,
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
    finds."""

    POLL_S = 0.15

    def __init__(self, registry, serial=SERIAL, output_id="HDMI-A-1", offered=OFFERED):
        self.registry, self.serial, self.output_id, self.offered = registry, serial, output_id, offered
        self.presenting = True
        self.buttons = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def answer(self):
        document = _picture(self.registry, self.serial, self.output_id)
        results = {code: HardwareResult(WriteResult.TAKEN, 60, value) for code, value in document.hardware.items()}
        read = {**document.hardware, **self.buttons}
        offered = tuple(replace(setting, current=read.get(setting.code, setting.current)) for setting in self.offered)
        _picture_report(self.registry, offered=offered, for_change=document.picture_change, results=results,
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
        expect(_row(page, DISPLAY, "Brightness")).to_contain_text("61 % (61 of 100)")
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


ACTS_ABOVE = "acts above 100 % brightness"


def test_black_floor_is_sent_in_tenths_and_reads_as_a_percent_once_kept(page, registry):
    """Print look R2 tracer: Black floor 2.0 % of white is sent as 20 tenths, presented, kept by Done,
    and the row reads 2.0 % from Central's stored value.

    Mutation: send the percent (2) instead of tenths -> the body assertion fails."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, PHOTO_WALL, "Black floor")).to_contain_text("0.0 % of white · 0.0 % changes nothing")
        software = _edits(page, "software-edits")
        _slider(page, PHOTO_WALL, "Black floor").fill("2")
        expect(_row(page, PHOTO_WALL, "Black floor")).to_contain_text("2.0 % of white")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        assert [(body["setting"], body["value"]) for _at, body in software] == [("black-floor", 20)], software
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _display(registry)["software_black_floor_tenths"] == 20
        page.reload()
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, PHOTO_WALL, "Black floor")).to_contain_text("2.0 % of white")


def test_saturation_drags_at_most_two_edits_a_second_and_revert_puts_back_the_stored_values(page, registry):
    """Live model on the print look: twenty steps of Saturation over about two seconds send at most
    two edits a second, the last where it stopped; Revert puts back the stored saturation and
    soft highlights.

    Mutation: drop the send on release (onCommit) -> the last value (80) never arrives."""
    _seen(registry)
    _store(registry, software_saturation_percent=100, software_highlight_rolloff_percent=30)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        edits = _edits(page, "software-edits")
        started = time.monotonic()
        saturation = _press(page, PHOTO_WALL, "Saturation", "ArrowLeft", 20, pause_ms=100)
        took = time.monotonic() - started
        expect(saturation).to_have_value("80")
        rolloff = _press(page, PHOTO_WALL, "Soft highlights", "ArrowRight", 5)
        expect(rolloff).to_have_value("35")
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        page.wait_for_timeout(600)
        sent = [body for _at, body in edits if body["setting"] == "saturation"]
        times = [at for at, _body in edits]
        assert sent and sent[-1]["value"] == 80, edits
        assert len(edits) <= 2 * took + 4, (len(edits), took)
        assert all(later - earlier >= 0.45 for earlier, later in zip(times, times[1:])), times
        assert _off_thread(_picture, registry).software.saturation_percent == 80

        bar.get_by_role("button", name="Revert", exact=True).click()
        expect(saturation).to_have_value("100")
        expect(rolloff).to_have_value("30")
        expect(bar.get_by_role("button", name="Revert", exact=True)).to_be_disabled()
        stored = _display(registry)
        assert (stored["software_saturation_percent"], stored["software_highlight_rolloff_percent"]) == (100, 30)


def test_soft_highlights_says_where_it_acts_only_while_nothing_passes_white(page, registry):
    """Soft highlights acts only above 100 % brightness (or saturation): at 100 % both, its row says
    so; with Photo Wall brightness at 120 % it does not.

    Mutation: show the words always -> the second assertion fails."""
    _seen(registry)
    with FakePi(registry), operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        expect(_bar(page).get_by_role("status")).to_have_text(PRESENTED)
        expect(_row(page, PHOTO_WALL, "Soft highlights")).to_contain_text(ACTS_ABOVE)
        _slider(page, PHOTO_WALL, "Brightness").fill("120")
        expect(_row(page, PHOTO_WALL, "Soft highlights")).not_to_contain_text(ACTS_ABOVE)
        expect(_row(page, PHOTO_WALL, "Saturation")).to_contain_text("100 % · 100 % changes nothing")


def test_the_lut_picker_sends_the_profiles_digest_and_done_waits_until_the_pi_shows_it(page, registry):
    """Print look R3: the Photo Wall section lists None and each LUT on the list; choosing one sends
    its paper profile's digest to lut-edits, LUT amount appears only with a LUT, the tab says it is
    waiting for the Pi until the Pi presents it, and Done keeps it.

    Mutation: send the LUT's name instead of its digest -> Central's 422, the choice is dropped."""
    luts = PgLuts(registry.db, registry.clock)
    paper, _ = luts.add_lut(_as_printer(_monitor()))
    reduced, _ = luts.add_lut(_reduced_gamut())
    # Both fixtures are LittleCMS's sRGB under its own name: the list orders by name, then digest.
    listed = [lut.name for lut in sorted((paper, reduced), key=lambda lut: (lut.name, lut.profile_sha256))]
    _seen(registry)
    with FakePi(registry) as pi, operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        open_frame(page, FRAME, "picture")
        bar = _bar(page)
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        picker = _group(page, PHOTO_WALL).get_by_role("combobox", name="LUT", exact=True)
        expect(picker.locator("option")).to_have_text(["None", *listed])
        expect(picker).to_have_value("none")
        expect(_slider(page, PHOTO_WALL, "LUT amount")).to_have_count(0)

        sent = _edits(page, "lut-edits")
        pi.presenting = False
        picker.select_option(value=paper.profile_sha256)
        expect(bar.get_by_role("status")).to_have_text(WAITING)
        expect(_slider(page, PHOTO_WALL, "LUT amount")).to_have_value("100")
        expect(_group(page, PHOTO_WALL)).to_contain_text("Done waits for it")
        page.wait_for_timeout(1200)
        expect(bar.get_by_role("button", name="Done", exact=True)).to_be_disabled()
        assert [body["profile_sha256"] for _at, body in sent] == [paper.profile_sha256], sent
        assert _off_thread(_picture, registry).software.lut.profile_sha256 == paper.profile_sha256

        pi.presenting = True
        expect(bar.get_by_role("status")).to_have_text(PRESENTED)
        bar.get_by_role("button", name="Done", exact=True).click()
        expect(_picture_tab(page).get_by_role("status").filter(has_text="Saved at")).to_be_visible()
        assert _display(registry)["software_lut_profile"] == paper.profile_sha256
        expect(picker).to_have_value(paper.profile_sha256)


def _positions(registry):
    return {frame.id: (frame.calibration.model_dump(mode="json"), frame.readiness)
            for frame in registry.inventory().frames}


def test_copy_to_two_frames_copies_photo_walls_values_and_never_position(page, registry):
    """Copy (#119): Photo Wall's four of this Frame's display go to two other Frames' displays, and
    each display setting set here where the target offers it the same way (hall: brightness, a level
    of 100 on both); stairs' Pi has not said what its display offers, so brightness is skipped there
    and named. Both Frames' position (fit, corners, readiness) stays as it was."""
    player = _seen(registry)
    _seen(registry, "hall", output_id="HDMI-A-2", player=player)
    _seen(registry, "stairs", serial="pi-stairs", offered=None)
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
        expect(results.locator("p", has_text="Frame hall")).to_have_text("Frame hall: Copied")
        expect(results.locator("p", has_text="Frame stairs")).to_have_text("Frame stairs: Copied; skipped: Brightness")

    assert sent == [{"to_frames": ["hall", "stairs"]}]
    for frame, hardware in (("hall", {"10": 70, "12": 10}), ("stairs", {})):
        display = _display(registry, frame)
        assert (display["software_brightness_percent"], display["software_contrast_percent"],
                display["colour_temperature_kelvin"], display["gamma_tenths"]) == (120, 30, 5000, 24)
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
