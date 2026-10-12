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
    kelvin_multipliers,
    operations_for,
    write_order,
)
from appliance.display_host.picture_list import (
    ASK_AGAIN_SECONDS,
    FileOfferedCache,
    PictureListAsker,
)
from contracts.node_frame import PictureOperations, decode_picture_operations, neutral_operations
from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    RangeKind,
    SoftwarePicture,
    WriteResult,
)
from player.geometry import final_pixel

IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)   # the spike's monitor
LEVEL_100 = HardwareRange(RangeKind.LEVEL, 100, ())

# Charity's table (bbr_color.txt 2001-Jun-22, 2deg rows), copied by hand for the cases tested.
CHARITY = {3000: (1.0000, 0.4870, 0.1411), 6500: (1.0000, 0.9436, 0.9621), 9300: (0.6703, 0.7362, 1.0000)}


class Shell:
    """The shell's `picture` control op: keeps every call."""

    def __init__(self) -> None:
        self.sent: list[tuple[PictureOperations, str]] = []
        self.lock = threading.Lock()

    def picture(self, operations, *, timeout, brightness):
        with self.lock:
            self.sent.append((operations, brightness))
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


def software(brightness: int = 100, contrast: int = 100, kelvin: int = 6500, gamma: int = 22) -> SoftwarePicture:
    return SoftwarePicture(brightness, contrast, kelvin, gamma)


def document(picture_change: int, hardware: dict[str, int] | None = None, wanted: SoftwarePicture = NEUTRAL_SOFTWARE,
             *, output_id: str = OUTPUT) -> PictureDocument:
    return PictureDocument(output_id, picture_change, hardware or {}, wanted)


class Running:
    def __init__(self, display: Display) -> None:
        self.display = display
        self.shell = Shell()
        self.reports = Reports()
        self.clock = Clock()
        applied = display.root / "applied"       # the controller's preserved runtime directory
        applied.mkdir(exist_ok=True)
        self.controller = PictureController(DdcPictureAdapter(display.channel), PictureListAsker(display.channel),
                                            FileOfferedCache(applied),
                                            self.shell, self.reports, FileAppliedPictures(applied), self.clock)

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
    software brightness to 114. The ask offers 10 as a level of 100 at 60; the Pi reads 10, writes
    46, reads it back; the shell gets light factor 1.14; the report names the change with 10 taken
    60 -> 46, and the change once the shell presented it. Mutation probe: `operations_for` divides
    by 50 -> the light factor is 2.28."""
    run = running()
    assert run.offered() == (HardwareSetting("10", LEVEL_100, 60), HardwareSetting("12", LEVEL_100, 50))
    run.controller.document(OUTPUT, document(8, {"10": 46}, software(brightness=114)))
    report = run.reported(8)
    assert picture_reads(display) == [["getvcp", "10"], ["setvcp", "10", "46"], ["getvcp", "10"]]
    assert report.results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
    assert report.identity == IDENTITY and report.presented_change is None
    until(lambda: run.shell.last()[1] == "Brightness: display 46 % · Photo Wall 114 %")
    operations, _line = run.shell.last()
    assert operations == PictureOperations(OUTPUT, 8, (1.0, 1.0, 1.0), 1.14, 1.0, 1.0)
    run.controller.presented(OUTPUT, 8)
    assert run.reports.latest().presented_change == 8


def test_the_operations_go_to_the_shell_before_any_hardware_write(display, running):
    display.script(write_seconds=1.0)
    run = running()
    run.offered()
    run.controller.document(OUTPUT, document(2, {"10": 46}, software(contrast=80)))
    until(lambda: display.writes() == [["10", "46"]], timeout=5)
    assert run.shell.sent and run.shell.sent[0][0].contrast_slope == 0.8   # while the write still runs
    assert run.reports.latest().for_change is None
    run.reported(2)


def test_a_display_that_reads_back_another_value_did_not_take_it(display, running):
    display.script(take=False)
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}, software(brightness=90)))
    report = run.reported(1)
    assert display.writes() == [["10", "46"]]
    assert report.results == {"10": HardwareResult(WriteResult.NOT_TAKEN, 60, 60)}
    until(lambda: run.shell.last()[1] == "Brightness: the display did not take it · Photo Wall 90 %")
    assert run.shell.last()[0].light_factor == 0.9      # the software group never leans on the hardware


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
    run.controller.document(OUTPUT, document(1, {}, software(brightness=40)))
    report = run.reported(1)
    assert report.results == {} and report.identity is None and report.offered is None
    assert display.calls() == []
    assert run.shell.last() == (operations_for(OUTPUT, 1, software(brightness=40)),
                                "Brightness: as the display has it · Photo Wall 40 %")


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

def test_a_batch_that_does_not_finish_leaves_the_list_unknown_until_the_next_ask(display, running):
    """Acceptance 5: one batch's `read_many` is None (its getvcp hangs past the hold) -> offered
    None and nothing written; the next ask, ASK_AGAIN_SECONDS later, completes it and the waiting
    change is carried out. Mutation probe: `PictureListAsker.ask` returns the finished batches
    when one is None -> offered is reported known."""
    display.script(hang=["16"], hang_seconds=4)          # the list's second batch
    run = running()
    run.controller.document(OUTPUT, document(1, {"10": 46}))
    until(lambda: sum(1 for args in display.started() if "16" in args) == 1)
    time.sleep(2.0)                                       # the cut batch, and a tick to report it
    assert all(report.offered is None for report in run.reports.puts)
    assert display.writes() == [] and run.reports.latest().for_change is None
    asks = len(display.started())
    time.sleep(1.2)
    assert len(display.started()) == asks                 # not asked again before ASK_AGAIN_SECONDS
    display.script(hang=[])
    run.clock.now += ASK_AGAIN_SECONDS
    report = run.reported(1)
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
    multipliers = kelvin_multipliers(kelvin)
    assert multipliers == pytest.approx(expected, abs=1e-3)
    assert max(multipliers) == 1.0
    operations = operations_for(OUTPUT, 1, software(kelvin=kelvin))
    assert operations.rgb_multipliers == multipliers
    white = final_pixel((1.0, 1.0, 1.0), operations, inside=True)[:3]
    assert white == pytest.approx([_encoded(value) for value in expected], abs=2e-3)


def test_the_neutral_values_change_nothing_and_the_ranges_hold():
    assert kelvin_multipliers(6500) == (1.0, 1.0, 1.0)
    assert operations_for(OUTPUT, 3, NEUTRAL_SOFTWARE) == neutral_operations(OUTPUT, 3)
    widest = operations_for(OUTPUT, 3, software(150, 0, 6500, 18))
    assert (widest.light_factor, widest.contrast_slope, widest.gamma_exponent) == (1.5, 0.0, 18 / 22)
    for kelvin in range(3000, 9301, 100):
        assert max(kelvin_multipliers(kelvin)) == 1.0
    for kelvin in (2900, 6550, 9400, 6500.0, True):
        with pytest.raises(ValueError, match="^kelvin$"):
            kelvin_multipliers(kelvin)


def test_the_base_page_names_each_brightness_and_never_the_software_as_the_panels():
    assert brightness_line(114, DisplayBrightness(46, True, True)) == "Brightness: display 46 % · Photo Wall 114 %"
    assert brightness_line(100, DisplayBrightness(60, False, True)) == "Brightness: display 60 % · Photo Wall 100 %"
    assert brightness_line(114, DisplayBrightness(60, True, False)) == (
        "Brightness: the display did not take it · Photo Wall 114 %")
    assert brightness_line(40, DisplayBrightness(None, False, True)) == "Brightness: as the display has it · Photo Wall 40 %"


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
    run.controller.document(OTHER, document(2, {}, software(brightness=80), output_id=OTHER))
    assert run.shell.sent == []                         # nothing before start
    run.controller.start()
    try:
        run.reported(4)
        run.reported(2, OTHER)
        until(lambda: {operations.output_id for operations, _ in run.shell.sent} == {OUTPUT, OTHER})
        assert display.writes() == [["10", "46"]]
        until(lambda: (OUTPUT, "Brightness: display 46 % · Photo Wall 100 %")
              in [(operations.output_id, line) for operations, line in run.shell.sent])
        sent = len(run.shell.sent)
        run.controller.shell_output(OUTPUT)
        until(lambda: len(run.shell.sent) == sent + 1)
        operations, line = run.shell.last()
        assert operations == PictureOperations(OUTPUT, 4, (1.0, 1.0, 1.0), 1.0, 0.5, 1.0)
        assert line == "Brightness: display 46 % · Photo Wall 100 %"
        time.sleep(0.3)
        assert display.writes() == [["10", "46"]]       # the re-send repeats no DDC/CI write
    finally:
        run.controller.stop()


def test_presented_follows_the_shells_echo_and_equal_operations_count_at_once(display, running):
    run = running()
    run.controller.document(OUTPUT, document(6, {"10": 46}, software(brightness=40)))
    assert run.reported(6).presented_change is None
    run.controller.presented(OUTPUT, None)               # a v3 tag: nothing
    run.controller.presented(OUTPUT, 6)
    assert run.reports.latest().presented_change == 6
    run.controller.document(OUTPUT, document(7, {"10": 70}, software(brightness=40)))   # same operations
    until(lambda: run.reports.latest().presented_change == 7)
    run.controller.document(OUTPUT, document(8, {"10": 70}, software(brightness=30)))   # new operations
    report = run.reported(8)
    assert report.presented_change == 7
    run.controller.presented(OUTPUT, 8)
    assert run.reports.latest().presented_change == 8


def test_an_older_or_equal_picture_change_writes_nothing(display, running):
    run = running()
    run.controller.document(OUTPUT, document(3, {"10": 46}))
    run.reported(3)
    sent = len(run.shell.sent)
    display.vcp("10", 35, 100)                          # someone on the display's buttons
    run.controller.document(OUTPUT, document(3, {"10": 46}))
    run.controller.document(OUTPUT, document(2, {"10": 10}))
    time.sleep(0.5)
    assert display.writes() == [["10", "46"]] and len(run.shell.sent) == sent


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
    time.sleep(0.5)
    assert display.calls() == [] and run.reports.latest().for_change is None   # waits for a display
    run.connect()
    report = run.reported(1)
    assert report.results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
    asks = sum(1 for args in display.started() if len(args) > 2 and args[0] == "getvcp")
    run.controller.output(OUTPUT, connected=False, identity=None)           # a blip
    run.connect()
    time.sleep(0.5)
    assert sum(1 for args in display.started() if len(args) > 2 and args[0] == "getvcp") == asks
    assert display.writes() == [["10", "46"]]                               # the same display: no write
    display.vcp("10", 30, 100)
    run.connect(identity=DisplayIdentity("ABC", 1, "Other", None))         # another display
    until(lambda: display.writes() == [["10", "46"], ["10", "46"]])
    assert sum(1 for args in display.started() if len(args) > 2 and args[0] == "getvcp") == 2 * asks


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
        operations = PictureOperations(OUTPUT, 6, (1.0, 0.5, 0.25), 0.8, 1.0, 1.0)
        assert backend.picture(operations, timeout=2, brightness="Brightness: as the display has it")
        playing.join(5)
        request = asked[0]
        assert (request["op"], request["output_id"], request["output"]) == ("picture", OUTPUT, output)
        assert decode_picture_operations(request["operations"]) == operations
        assert request["brightness"] == "Brightness: as the display has it"
        assert not backend.picture(neutral_operations("HDMI-A-2", 1), timeout=1, brightness="")   # no such Output


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
    until(lambda: run.shell.last()[1] == "Brightness: display 30 % · Photo Wall 100 %")
    assert display.writes() == [["10", "46"]] and run.reports.latest().for_change == 1
    reads = len(display.started())
    time.sleep(1.2)
    assert len(display.started()) == reads              # once per REREAD_SECONDS, not every tick
    restarted = Running(display)                        # the kept list carries the latest read
    assert restarted.controller._offered.get(IDENTITY) == run.reports.latest().offered
