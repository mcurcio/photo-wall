"""The Pi carries out each picture document, the display's own settings and Photo Wall's each on
its own (decision 0022; the picture split, slices S1 and S3; appliance/display_host/picture.py,
from roadmap 1c's controller).

`ddcutil` is a real executable (node.display.fake_ddcutil): a fake script that records every call
and answers from a scripted display, run through the production `run_tool`, the real
`DdcChannel`, the real `PictureListAsker` and `DdcPictureAdapter`. The shell and the reports are
fakes; the controller counts on a fake clock.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from uuid import uuid4

import pytest
from node.display.fake_ddcutil import OTHER, OUTPUT, RECORDED_CAPABILITIES, Display

from appliance.display_host.picture import (
    REREAD_SECONDS,
    WRITE_GAP_SECONDS,
    DdcPictureAdapter,
    DisplayBrightness,
    FileAppliedPictures,
    PictureController,
    brightness_line,
    judged,
    operations_for,
    write_order,
)
from appliance.display_host.picture_list import (
    ASK_AGAIN_SECONDS,
    FileOfferedCache,
    PictureListAsker,
)
from appliance.display_host.signal_range import RangeUnknown
from contracts.frame_lighting import FrameLighting, Paper
from contracts.kelvin import kelvin_white
from contracts.node_frame import (
    PictureOperations,
    decode_picture_operations,
    encode_picture_operations,
    neutral_operations,
)
from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    UPGRADE_DISPLAY_LIGHT,
    DisplayLight,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    RangeKind,
    SoftwarePicture,
    WriteResult,
)
from contracts.tuning import TestPattern
from player.geometry import final_pixel, picture_lights

IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)   # the spike's monitor
LEVEL_100 = HardwareRange(RangeKind.LEVEL, 100, ())

# Charity's table (bbr_color.txt 2001-Jun-22, 2deg rows), copied by hand for the cases tested.
CHARITY = {3000: (1.0000, 0.4870, 0.1411), 6500: (1.0000, 0.9436, 0.9621), 9300: (0.6703, 0.7362, 1.0000)}


class Shell:
    """The shell's `picture` control op: keeps every call, its operations decoded, and each call's
    `drawing` beside it."""

    def __init__(self) -> None:
        self.sent: list[tuple[PictureOperations, str]] = []
        self.drawing: list[bool] = []
        self.lock = threading.Lock()

    def picture(self, output_id, operations, brightness, *, drawing, timeout):
        decoded = decode_picture_operations(operations)
        assert decoded.output_id == output_id
        with self.lock:
            self.sent.append((decoded, brightness))
            self.drawing.append(drawing)
        return True

    def last(self) -> tuple[PictureOperations, str]:
        with self.lock:
            return self.sent[-1]


class Reports:
    """`PictureReports`: keeps every report put."""

    def __init__(self) -> None:
        self.puts: list[PictureReport] = []

    def put_picture(self, report: PictureReport) -> None:
        self.puts.append(report)

    def latest(self, output_id: str = OUTPUT) -> PictureReport | None:
        return next((report for report in reversed(self.puts) if report.output_id == output_id), None)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def until(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def software(contrast: int = 100, kelvin: int = 6500, gamma: int = 22) -> SoftwarePicture:
    return SoftwarePicture(contrast_percent=contrast, colour_temperature_kelvin=kelvin, gamma_tenths=gamma)


LIGHT = UPGRADE_DISPLAY_LIGHT


def document(picture_change: int, hardware: dict[str, int] | None = None, wanted: SoftwarePicture = NEUTRAL_SOFTWARE,
             *, output_id: str = OUTPUT, light: DisplayLight = LIGHT) -> PictureDocument:
    return PictureDocument(output_id, picture_change, hardware or {}, wanted, light=light)


class NoRange:
    """`SignalPort` of an output with no range property (as a Virtual output): never a restart."""

    def in_force(self, output_id):
        return RangeUnknown.NO_PROPERTY

    def requested(self, output_id):
        return None

    def restart(self, ranges):
        raise AssertionError("no restart for an output without a range")


class Running:
    def __init__(self, display: Display, signal=None) -> None:
        self.display = display
        self.shell = Shell()
        self.reports = Reports()
        self.clock = Clock()
        applied = display.root / "applied"       # the controller's preserved runtime directory
        applied.mkdir(exist_ok=True)
        self.controller = PictureController(DdcPictureAdapter(display.channel), PictureListAsker(display.channel),
                                            FileOfferedCache(applied),
                                            self.shell, self.reports, FileAppliedPictures(applied), self.clock,
                                            NoRange() if signal is None else signal)

    def connect(self, output_id: str = OUTPUT, identity: DisplayIdentity = IDENTITY) -> None:
        self.controller.output(output_id, connected=True, identity=identity)

    def reported(self, change: int, output_id: str = OUTPUT) -> PictureReport:
        until(lambda: (latest := self.reports.latest(output_id)) is not None and latest.for_change == change)
        return self.reports.latest(output_id)

    def offered(self, output_id: str = OUTPUT) -> tuple[HardwareSetting, ...]:
        until(lambda: (latest := self.reports.latest(output_id)) is not None and latest.offered is not None)
        return self.reports.latest(output_id).offered


@pytest.fixture
def display(tmp_path):
    return Display(tmp_path)


@pytest.fixture
def running(display):
    started: list[Running] = []

    def start(connect: bool = True) -> Running:
        subject = Running(display)
        if connect:
            subject.connect()
        subject.controller.start()
        started.append(subject)
        return subject
    yield start
    for subject in started:
        subject.controller.stop()


def picture_reads(display: Display) -> list[list[str]]:
    """Every command after the list's ask: the controller's own reads and writes."""
    return [args for args in display.started() if not (args[0] == "getvcp" and len(args) > 2)]


# The tracer through the Pi ------------------------------------------------------------------------

def test_hardware_46_and_software_114_each_land_on_their_own(display, running):
    """Acceptance 3: the display reads 10 as 60 of 100; a document sets hardware 10 to 46 and
    software contrast to 114. The ask offers 10 as a level of 100 at 60; the Pi reads 10, writes
    46, reads it back; the shell gets contrast slope 1.14 with the Display's light; the report names
    the change with 10 taken 60 -> 46, and the change once the shell presented it. Mutation probe:
    `operations_for` divides by 50 -> the slope is 2.28."""
    run = running()
    assert run.offered() == (HardwareSetting("10", LEVEL_100, 60), HardwareSetting("12", LEVEL_100, 50))
    run.controller.document(OUTPUT, document(8, {"10": 46}, software(contrast=114)))
    report = run.reported(8)
    assert picture_reads(display) == [["getvcp", "10"], ["setvcp", "10", "46"], ["getvcp", "10"]]
    assert report.results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
    assert report.identity == IDENTITY and report.presented_change is None
    until(lambda: run.shell.last()[1] == "Brightness: display 46 %")
    operations, _line = run.shell.last()
    assert operations == PictureOperations(OUTPUT, 8, (1.0, 1.0, 1.0), 1.14, 1.0, LIGHT)
    run.controller.presented(OUTPUT, 8)
    assert run.reports.latest().presented_change == 8


def test_the_operations_go_to_the_shell_before_any_hardware_write(display, running):
    display.script(write_seconds=1.0)
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(2, {"10": 46}, software(contrast=80)))
    until(lambda: display.writes() == [["10", "46"]], timeout=5)
    until(lambda: bool(run.shell.sent), timeout=0.5)    # the shell worker never waits on the display
    assert run.shell.sent[0][0].contrast_slope == 0.8
    assert [call["at"] for call in display.calls() if call["args"][0] == "setvcp"] == ["start"]   # still writing
    assert run.reports.latest().for_change is None
    run.reported(2)


def test_a_display_that_reads_back_another_value_did_not_take_it(display, running):
    display.script(take=False)
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}, software(contrast=90)))
    report = run.reported(1)
    assert display.writes() == [["10", "46"]]
    assert report.results == {"10": HardwareResult(WriteResult.NOT_TAKEN, 60, 60)}
    until(lambda: run.shell.last()[1] == "Brightness: the display did not take it")
    assert run.shell.last()[0].contrast_slope == 0.9    # the software group never leans on the hardware


def test_a_write_with_no_read_back_is_no_answer(display, running):
    display.script(mute_after_write=True)
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    report = run.reported(1)
    assert report.results == {"10": HardwareResult(WriteResult.NO_ANSWER, 60, None)}


# Unset writes nothing ------------------------------------------------------------------------------

def test_only_the_codes_set_and_offered_are_written(display, running):
    """Acceptance 4: offered 10 and 12, the document sets only 10 -> no `setvcp 12`; a code the
    display does not offer (16) is never written."""
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(1, {"10": 46, "16": 30}))
    report = run.reported(1)
    assert display.writes() == [["10", "46"]]
    assert set(report.results) == {"10"}


def test_empty_hardware_is_carried_out_at_once_even_with_no_display(display, running):
    run = running(connect=False)
    run.controller.output(OUTPUT, connected=False, identity=None)
    run.controller.document(OUTPUT, document(1, {}, software(contrast=40)))
    report = run.reported(1)
    assert report.results == {} and report.identity is None and report.offered is None
    assert display.calls() == []
    until(lambda: run.shell.sent and run.shell.last() == (operations_for(OUTPUT, 1, software(contrast=40), LIGHT, None),
                                                           "Brightness: as the display has it"))


def test_a_value_the_display_already_has_or_past_its_range_is_not_written(display, running):
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 60, "12": 150}))
    report = run.reported(1)
    assert display.writes() == []
    assert report.results == {"12": HardwareResult(WriteResult.NOT_TAKEN, 50, 50)}   # 150 is past 12's 100


def test_a_list_setting_is_offered_read_only_with_its_byte(display, running):
    display.script(snc={"14": 5, "DC": 0})
    run = running()
    offered = {setting.code: setting for setting in run.offered()}
    assert offered["14"] == HardwareSetting("14", HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ()), 5)
    assert offered["DC"].current == 0 and list(offered) == ["10", "12", "14", "DC"]


# Unknown is never reported known --------------------------------------------------------------------

def test_a_batch_that_does_not_finish_leaves_the_list_unknown_until_the_next_ask(display, running, caplog):
    """Acceptance 5: one batch's `read_many` is None (its getvcp hangs past the hold) -> offered
    None and nothing written, while the change is answered with no result, so Photo Wall's group
    is not held by the display's (the fix pass: "a change is answered while its hardware waits");
    the next ask, ASK_AGAIN_SECONDS later, completes it and the waiting change is carried out.
    Mutation probes: `PictureListAsker.ask` returns the finished batches when one is None ->
    offered is reported known; `_carry_out` returns without `_answer` -> for_change stays None;
    no log for the cut batch -> fails."""
    display.script(hang=["16"], hang_seconds=4)          # the list's second batch
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    until(lambda: sum(1 for args in display.started() if "16" in args) == 1)
    time.sleep(2.0)                                       # the cut batch, and a tick to report it
    assert all(report.offered is None for report in run.reports.puts)
    assert display.writes() == []
    assert run.reports.latest().for_change == 1 and run.reports.latest().results == {}
    assert any("HDMI-A-1's getvcp 16 18 1A 59 did not finish" in record.getMessage()
               for record in caplog.records)             # logged with its Output, codes and time
    asks = len(display.started())
    time.sleep(1.2)
    assert len(display.started()) == asks                 # not asked again before ASK_AGAIN_SECONDS
    display.script(hang=[])
    run.clock.now += ASK_AGAIN_SECONDS
    until(lambda: run.reports.latest().results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
    report = run.reports.latest()
    assert report.offered is not None and {setting.code for setting in report.offered} == {"10", "12"}
    assert display.writes() == [["10", "46"]]


def test_a_display_with_no_answer_at_all_offers_nothing_and_nothing_is_written(display, running):
    display.script(answer=False)
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    report = run.reported(1)
    assert report.offered == () and report.results == {} and display.writes() == []


# The pure rules ----------------------------------------------------------------------------------

def test_judged_and_the_write_order():
    from appliance.display_host.ddc import VcpByte, VcpReading

    assert judged(46, VcpReading(46, 100)) is WriteResult.TAKEN
    assert judged(46, VcpReading(45, 100)) is WriteResult.NOT_TAKEN
    assert judged(5, VcpByte(5)) is WriteResult.TAKEN
    assert judged(5, None) is WriteResult.NO_ANSWER
    offered = (HardwareSetting("10", LEVEL_100, 1), HardwareSetting("12", LEVEL_100, 1),
               HardwareSetting("87", LEVEL_100, 1))
    assert write_order({"87": 1, "10": 2, "16": 3}, offered) == ("10", "87")
    preset = HardwareSetting("14", HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ()), 5)
    mode = HardwareSetting("DC", HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ()), 0)
    assert write_order({"87": 1, "DC": 0, "10": 2, "14": 5}, (*offered, preset, mode)) == ("14", "DC", "10", "87")


def _linear(encoded: float) -> float:
    """IEC 61966-2-1, written out here so the test does not lean on the code it checks."""
    return encoded / 12.92 if encoded <= 0.04045 else ((encoded + 0.055) / 1.055) ** 2.4


def _encoded(linear: float) -> float:
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055


@pytest.mark.parametrize("kelvin", [3000, 9300])
def test_colour_temperature_shows_white_as_charitys_table(kelvin):
    """Charity's R G B are gamma-encoded sRGB; the Player multiplies linear light. So the
    multipliers are the table's rows in linear light, relative to 6500 K, and white comes out of
    the final pass as the table's colour (relative to 6500 K, largest 1). Mutation probe: divide
    the encoded rows -> 3000 K white shows green 0.75 where the table's is 0.53."""
    ratio = [_linear(value) / _linear(neutral) for value, neutral in zip(CHARITY[kelvin], CHARITY[6500], strict=True)]
    expected = [value / max(ratio) for value in ratio]
    multipliers = kelvin_white(kelvin)
    assert multipliers == pytest.approx(expected, abs=1e-3)
    assert max(multipliers) == 1.0
    operations = operations_for(OUTPUT, 1, software(kelvin=kelvin), LIGHT, None)
    assert operations.rgb_multipliers == multipliers
    lights = picture_lights(FrameLighting(mat_depth_mm=0, lights=(), paper=Paper(whiteness=.9, ink_density=2)), None,
                            (598.0, 336.0), LIGHT)
    pattern = PictureOperations(**{**operations.__dict__, "test_pattern": TestPattern.WHITE})
    white = final_pixel((0.0, 0.0, 0.0), pattern, inside=True, lights=lights, x=.5, y=.5, opening_uv=(.5, .5))[:3]
    assert white == pytest.approx([_encoded(value) for value in expected], abs=2e-3)


def test_the_neutral_values_change_nothing_and_the_ranges_hold():
    assert kelvin_white(6500) == (1.0, 1.0, 1.0)
    assert operations_for(OUTPUT, 3, NEUTRAL_SOFTWARE, LIGHT, None) == neutral_operations(OUTPUT, 3, LIGHT)
    widest = operations_for(OUTPUT, 3, software(0, 6500, 18), LIGHT, None)
    assert (widest.contrast_slope, widest.gamma_exponent) == (0.0, 18 / 22)
    for kelvin in range(3000, 9301, 100):
        assert max(kelvin_white(kelvin)) == 1.0
    for kelvin in (1900, 6550, 10100, 6500.0, True):
        with pytest.raises(ValueError, match="^kelvin$"):
            kelvin_white(kelvin)


def test_the_displays_light_rides_the_operations_as_given():
    """The document's DisplayLight is the operations' light, and nothing else of the operations moves
    with it."""
    dim = DisplayLight(matched_lux=250.0, black=0.01)
    operations = operations_for(OUTPUT, 4, software(contrast=90), dim, None)
    assert operations.light == dim
    assert operations_for(OUTPUT, 4, software(contrast=90), LIGHT, None).contrast_slope == operations.contrast_slope


def test_the_base_page_names_the_displays_brightness_only():
    """Photo Wall has no brightness of its own since decision 0028: the line names the display's."""
    assert brightness_line(DisplayBrightness(46, True, True)) == "Brightness: display 46 %"
    assert brightness_line(DisplayBrightness(60, False, True)) == "Brightness: display 60 %"
    assert brightness_line(DisplayBrightness(60, True, False)) == "Brightness: the display did not take it"
    assert brightness_line(DisplayBrightness(None, False, True)) == "Brightness: as the display has it"


# Newest wins, re-sends, presented -------------------------------------------------------------------

def test_newest_wins_a_slow_display_gets_the_first_and_the_last_change_only(display, running):
    display.script(write_seconds=1.0)                   # within the picture hold, so it finishes
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(2, {"10": 20}))
    until(lambda: display.writes() == [["10", "20"]], timeout=5)
    for picture_change in range(3, 7):                  # four more within the first write's second
        run.controller.document(OUTPUT, document(picture_change, {"10": picture_change * 10 + 1}))
        time.sleep(0.05)
    assert [call["at"] for call in display.calls() if call["args"][0] == "setvcp"] == ["start"]   # still writing
    report = run.reported(6)
    assert display.writes() == [["10", "20"], ["10", "61"]]
    assert report.results == {"10": HardwareResult(WriteResult.TAKEN, 20, 61)}


def test_start_and_a_recreated_shell_output_re_send_operations_and_repeat_no_write(display, running):
    run = Running(display)
    run.connect()
    run.connect(OTHER)
    run.controller.document(OUTPUT, document(4, {"10": 46}, software(contrast=50)))
    run.controller.document(OTHER, document(2, {}, software(contrast=80), output_id=OTHER))
    assert run.shell.sent == []                         # nothing before start
    run.controller.start()
    try:
        run.reported(4)
        run.reported(2, OTHER)
        until(lambda: {operations.output_id for operations, _ in run.shell.sent} == {OUTPUT, OTHER})
        assert display.writes() == [["10", "46"]]
        until(lambda: (OUTPUT, "Brightness: display 46 %")
              in [(operations.output_id, line) for operations, line in run.shell.sent])
        sent = len(run.shell.sent)
        run.controller.shell_output(OUTPUT)
        until(lambda: len(run.shell.sent) == sent + 1)
        operations, line = run.shell.last()
        assert operations == PictureOperations(OUTPUT, 4, (1.0, 1.0, 1.0), 0.5, 1.0, LIGHT)
        assert line == "Brightness: display 46 %"
        time.sleep(0.3)
        assert display.writes() == [["10", "46"]]       # the re-send repeats no DDC/CI write
    finally:
        run.controller.stop()


def test_presented_follows_the_shells_echo_and_equal_operations_count_at_once(display, running):
    run = running()
    run.controller.document(OUTPUT, document(6, {"10": 46}, software(contrast=40)))
    assert run.reported(6).presented_change is None
    run.controller.presented(OUTPUT, None)               # a v3 tag: nothing
    run.controller.presented(OUTPUT, 6)
    assert run.reports.latest().presented_change == 6
    run.controller.document(OUTPUT, document(7, {"10": 70}, software(contrast=40)))   # same operations
    until(lambda: run.reports.latest().presented_change == 7)
    run.controller.document(OUTPUT, document(8, {"10": 70}, software(contrast=30)))   # new operations
    report = run.reported(8)
    assert report.presented_change == 7
    run.controller.presented(OUTPUT, 8)
    assert run.reports.latest().presented_change == 8


def test_a_change_to_the_displays_light_only_waits_for_the_shell_to_present_it(display, running):
    """A document that moves only the Display's light sends new operations to the shell, and Done
    waits: presented_change reaches it only when the shell echoes it. Mutation probe: compare only
    the Display's own steps in `_values` -> presented_change advances at once."""
    run = running()
    run.controller.document(OUTPUT, document(6, {}, NEUTRAL_SOFTWARE))
    run.reported(6)
    run.controller.presented(OUTPUT, 6)
    assert run.reports.latest().presented_change == 6
    matched = DisplayLight(matched_lux=400.0, black=LIGHT.black)
    run.controller.document(OUTPUT, document(7, {}, NEUTRAL_SOFTWARE, light=matched))
    until(lambda: run.shell.sent and run.shell.last()[0].picture_change == 7)
    assert run.shell.last()[0] == operations_for(OUTPUT, 7, NEUTRAL_SOFTWARE, matched, None)
    time.sleep(0.2)
    assert run.reported(7).presented_change == 6
    run.controller.presented(OUTPUT, 7)
    assert run.reports.latest().presented_change == 7


def test_an_older_or_equal_picture_change_writes_nothing(display, running):
    """Newest wins by picture_change: an equal or older change, whatever it carries, writes nothing
    and never reaches the shell. Change 3 settles first: its report comes before the line that
    follows its write (`_finish` reports, then refreshes the line), so the test waits for that
    line, never for the report alone. The forced re-send is the barrier: it carries whatever
    document the controller holds. Mutation probe: drop the `<=` check in `document` (or make it
    `<`) -> the re-send carries change 2 (or change 3 at 50 %)."""
    run = running()
    run.controller.document(OUTPUT, document(3, {"10": 46}))
    run.reported(3)
    settled = (operations_for(OUTPUT, 3, NEUTRAL_SOFTWARE, LIGHT, None), "Brightness: display 46 %")
    until(lambda: bool(run.shell.sent) and run.shell.last() == settled)
    sent = len(run.shell.sent)
    display.vcp("10", 35, 100)                          # someone on the display's buttons
    run.controller.document(OUTPUT, document(3, {"10": 46}, software(contrast=50)))
    run.controller.document(OUTPUT, document(2, {"10": 10}))
    run.controller.shell_output(OUTPUT)                 # the re-send: what the controller holds now
    until(lambda: len(run.shell.sent) == sent + 1)
    assert run.shell.last() == settled
    assert display.writes() == [["10", "46"]] and run.reports.latest().for_change == 3


def test_a_restarted_controller_does_not_write_a_change_the_display_already_had(display, running):
    """A Weston restart restarts the controller, which replays the stored document: the change it
    already carried out on this display is not written again. Mutation probe: forget the applied
    change (no `AppliedPictures` check) -> the restart writes 46 over the 35."""
    first = running()
    first.controller.document(OUTPUT, document(3, {"10": 46}))
    first.reported(3)
    first.controller.stop()
    display.vcp("10", 35, 100)                          # someone on the display's buttons
    again = running()
    again.controller.document(OUTPUT, document(3, {"10": 46}))
    report = again.reported(3)
    assert display.writes() == [["10", "46"]] and report.results == {}
    again.controller.document(OUTPUT, document(4, {"10": 70}))
    until(lambda: display.writes() == [["10", "46"], ["10", "70"]])


def test_writes_to_one_display_are_at_least_the_gap_apart(display, running):
    """At most two writes a second per display, however fast the changes come (some displays
    wear with each write). Mutation probe: drop the wait -> two setvcp start closer than the gap."""
    run = running()
    for change, value in enumerate((60, 61, 62, 63), start=1):
        run.controller.document(OUTPUT, document(change, {"10": value + 1, "12": value - 20}))
        run.reported(change)
    starts = sorted(call["time"] for call in display.calls()
                    if call["at"] == "start" and call["args"][0] == "setvcp")
    assert len(starts) == 8
    assert min(later - earlier for earlier, later in zip(starts, starts[1:], strict=False)) >= WRITE_GAP_SECONDS - 0.01


def test_another_display_is_asked_and_written_and_a_blip_is_not_asked_again(display, running):
    display.script(write_seconds=0.3)
    run = running(connect=False)
    run.controller.output(OUTPUT, connected=False, identity=None)
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    assert run.reported(1).results == {}                                    # the change is answered
    assert display.calls() == []                                            # the hardware waits for a display
    run.connect()
    until(lambda: run.reports.latest().results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
    run.controller.output(OUTPUT, connected=False, identity=None)           # a blip
    run.connect()
    time.sleep(0.5)
    assert asks(display) == 1
    assert display.writes() == [["10", "46"]]                               # the same display: no write
    display.vcp("10", 30, 100)
    run.connect(identity=DisplayIdentity("ABC", 1, "Other", None))         # another display
    until(lambda: display.writes() == [["10", "46"], ["10", "46"]])
    assert asks(display) == 2


# The backend's picture op over the shell's socket -------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the shell's control socket is SOCK_SEQPACKET with SO_PEERCRED (Linux)")
def test_the_backend_sends_the_picture_op_and_hands_the_shells_events_on():
    from appliance.display_host.weston import WestonBackend

    ours, shell = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    boot, incarnation = uuid4(), uuid4()
    output = {"kernel_boot_id": str(boot), "display_host_incarnation": str(incarnation),
              "output_id": OUTPUT, "connection_generation": 1, "mode_generation": 1}

    def send(value):
        shell.send(json.dumps(value).encode())

    send({"event": "hello", "protocol": 1, "boot_id": str(boot), "incarnation_id": str(incarnation)})
    send({"event": "output", "output": output, "connected": True, "width": 640, "height": 480, "power": "on"})
    heard = []

    class Events:
        def presented(self, output_id, change):
            heard.append(("presented", output_id, change))

        def shell_output(self, output_id):
            heard.append(("output", output_id))

    asked = []

    def play_shell():
        request = json.loads(shell.recv(16384))
        asked.append(request)
        send({"event": "response", "request_id": request["request_id"], "accepted": True})

    with ours, shell:
        backend = WestonBackend(ours, display_uid=os.getuid(), compositor_pid=os.getpid(),
                                verify_process=lambda grant: True)
        backend.picture_events = Events()
        backend.initialize()
        backend.dispatch()
        assert heard == [("output", OUTPUT)]
        playing = threading.Thread(target=play_shell)
        playing.start()
        operations = PictureOperations(OUTPUT, 6, (1.0, 0.5, 0.25), 0.8, 1.0, LIGHT)
        assert backend.picture(OUTPUT, encode_picture_operations(operations), "Brightness: as the display has it",
                               drawing=False, timeout=2)
        playing.join(5)
        request = asked[0]
        assert (request["op"], request["output_id"], request["output"]) == ("picture", OUTPUT, output)
        assert decode_picture_operations(request["operations"]) == operations
        assert request["brightness"] == "Brightness: as the display has it"
        assert request["drawing"] is False
        assert not backend.picture("HDMI-A-2", encode_picture_operations(neutral_operations("HDMI-A-2", 1, LIGHT)),
                                   "", drawing=False, timeout=1)   # no such Output


# List settings, the list kept, the re-read (slice S3) ---------------------------------------------

def asks(display: Display) -> int:
    """How many times the whole list was asked: its first batch (0C 10 12 14)."""
    return sum(1 for args in display.started() if args[:2] == ["getvcp", "0C"])


def test_a_preset_is_written_before_the_gains_it_moves(display, running):
    """Acceptance: hardware {14: 5, 16: 40} writes 14 (a CHOICE named by the capabilities string)
    before 16, as the byte (`setvcp 14 5`). Mutation probe: `write_order` puts levels first -> 16
    is written first."""
    display.script(snc={"14": 8}, vcp={"10": [60, 100], "16": [128, 255]}, capabilities=RECORDED_CAPABILITIES)
    run = running()
    offered = {setting.code: setting for setting in run.offered()}
    assert offered["14"].range.kind is RangeKind.CHOICE and offered["16"].range.maximum == 255
    run.controller.document(OUTPUT, document(1, {"16": 40, "14": 5}))
    report = run.reported(1)
    assert display.writes() == [["14", "5"], ["16", "40"]]
    assert report.results == {"14": HardwareResult(WriteResult.TAKEN, 8, 5), "16": HardwareResult(WriteResult.TAKEN, 128, 40)}


def test_a_choice_the_display_does_not_list_is_not_written(display, running):
    display.script(snc={"14": 8, "DC": 0}, capabilities=RECORDED_CAPABILITIES)
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(1, {"14": 6, "DC": 2}))      # 6 not listed; DC lists none
    report = run.reported(1)
    assert display.writes() == []
    assert report.results == {"14": HardwareResult(WriteResult.NOT_TAKEN, 8, 8),
                              "DC": HardwareResult(WriteResult.NOT_TAKEN, 0, 0)}


def test_a_restarted_controller_reports_the_kept_list_without_asking_and_another_display_asks(display, running):
    """Acceptance: a restarted controller with the same identity reports the kept list without
    asking (the fake counts the asks); another identity asks."""
    first = running()
    kept = first.offered()
    first.controller.stop()
    assert asks(display) == 1
    again = running()
    assert again.offered() == kept
    time.sleep(0.3)
    assert asks(display) == 1
    again.connect(identity=DisplayIdentity("ABC", 1, "Other", None))
    until(lambda: asks(display) == 2)


def test_the_display_is_read_again_and_a_change_on_its_buttons_is_reported_not_undone(display, running):
    """The owner's addition (2026-10-12): the Pi reads the held list again every REREAD_SECONDS and
    reports each current, so Central can say "Changed on the display's buttons"; it writes nothing
    back. A write's own read-back is the setting's current at once."""
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    run.reported(1)
    until(lambda: {setting.code: setting.current for setting in run.reports.latest().offered}["10"] == 46)
    display.vcp("10", 30, 100)                          # someone on the display's buttons
    time.sleep(1.2)
    assert {setting.code: setting.current for setting in run.reports.latest().offered}["10"] == 46   # not yet due
    run.clock.now += REREAD_SECONDS
    until(lambda: {setting.code: setting.current for setting in run.reports.latest().offered}["10"] == 30)
    until(lambda: run.shell.last()[1] == "Brightness: display 30 %")
    assert display.writes() == [["10", "46"]] and run.reports.latest().for_change == 1
    reads = len(display.started())
    time.sleep(1.2)
    assert len(display.started()) == reads              # once per REREAD_SECONDS, not every tick
    restarted = Running(display)                        # the kept list carries the latest read
    assert restarted.controller._offered.get(IDENTITY) == run.reports.latest().offered


# The two groups are not coupled in time; the fix pass after review --------------------------------

def test_photo_walls_change_reaches_the_shell_while_a_display_write_is_running(display, running):
    """A software change that arrives while the hardware worker waits on a slow write reaches the
    shell at once, not after the write. Mutation probe: send the operations from the hardware worker
    (no shell worker) -> change 2 reaches the shell only after the 1 s write."""
    display.script(write_seconds=1.0)
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    until(lambda: display.writes() == [["10", "46"]], timeout=5)
    run.controller.document(OUTPUT, document(2, {"10": 46}, software(contrast=50)))
    until(lambda: any(operations.picture_change == 2 for operations, _ in run.shell.sent), timeout=0.5)
    assert [call["at"] for call in display.calls() if call["args"][0] == "setvcp"] == ["start"]   # still writing
    run.reported(2)


def test_a_change_to_photo_walls_group_only_touches_no_ddc(display, running):
    """A change that moves only the software group writes and reads nothing on the display, and the
    report keeps each untouched code's result; a later change of one code writes only that code.
    Mutation probe: carry out every set code on each change -> getvcp 10 and 12 run again."""
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(1, {"10": 46, "12": 40}))
    first = run.reported(1).results
    commands = len(display.started())
    run.controller.document(OUTPUT, document(2, {"10": 46, "12": 40}, software(contrast=120)))
    assert run.reported(2).results == first
    assert len(display.started()) == commands
    run.controller.document(OUTPUT, document(3, {"10": 46, "12": 45}))
    report = run.reported(3)
    assert display.started()[commands:] == [["getvcp", "12"], ["setvcp", "12", "45"], ["getvcp", "12"]]
    assert report.results == {"10": first["10"], "12": HardwareResult(WriteResult.TAKEN, 40, 45)}


def test_a_display_that_comes_back_is_read_again_at_once(display, running):
    """1c read a set value again at each appearance: a blip with the same identity reads the held
    list at once, so a value changed while away shows without waiting REREAD_SECONDS. Mutation
    probe: leave read_after as it was on an appearance -> 10 stays 60 until the clock moves."""
    run = running()
    run.offered()
    run.controller.output(OUTPUT, connected=False, identity=None)
    display.vcp("10", 30, 100)                          # changed while it was away
    run.connect()
    until(lambda: {setting.code: setting.current for setting in run.reports.latest().offered}["10"] == 30, timeout=5)
    assert display.writes() == []


def test_a_restart_before_any_re_read_reports_the_written_value(display, running):
    """E-PS-S3: the kept list is put after every write, so a controller restarted before a re-read
    reports the value it wrote, not the ask's. Mutation probe: drop the put after the writes -> the
    restarted controller reports 10 at 60."""
    first = running()
    first.offered()
    first.controller.document(OUTPUT, document(1, {"10": 46}))
    first.reported(1)
    first.controller.stop()
    again = running()
    assert {setting.code: setting.current for setting in again.offered()}["10"] == 46
