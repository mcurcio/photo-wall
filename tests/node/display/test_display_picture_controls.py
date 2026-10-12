"""The Pi carries out each Output document's picture part (roadmap 1c, slice P1; run ledger
.claude/runs/display-1c.md; appliance/display_host/picture.py).

`ddcutil` is a real executable: a fake script that records every call with its start and end time
and answers from a scripted display (VCP 0x10 and 0x12 with their maxima, whether it answers,
whether a write takes, how long a write lasts), run through the production `run_tool` and the real
`DdcChannel`. The shell and the reports are fakes; the controller counts on a fake clock.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from node.display.fake_display_state import SOURCE as STATE_SOURCE
from node.display.fake_display_state import changing_state

from appliance.display_host.ddc import DdcChannel, VcpReading
from appliance.display_host.output_report import OutputReports
from appliance.display_host.picture import (
    REREAD_SECONDS,
    WRITE_GAP_SECONDS,
    DdcPictureAdapter,
    FileAppliedPictures,
    PictureController,
    brightness_line,
    kelvin_multipliers,
    operations_for,
    percent_of,
    vcp_for,
)
from appliance.display_host.power_methods import DDCUTIL, DdcCiAdapter, run_tool
from contracts.node_frame import PictureOperations, decode_picture_operations, neutral_operations
from contracts.node_output import (
    AS_THE_DISPLAY_HAS_IT,
    BEST_DETECTED,
    ActsOn,
    Darkness,
    DisplayIdentity,
    OutputDocument,
    OutputPicture,
    OutputPower,
    OutputReport,
    PictureReport,
    PictureSetting,
    Power,
    SettingReport,
    WriteResult,
)
from player.geometry import final_pixel

OUTPUT = "HDMI-A-1"
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)   # the spike's monitor
UNSET = PictureSetting(ActsOn.DISPLAY, AS_THE_DISPLAY_HAS_IT)

# Charity's table (bbr_color.txt 2001-Jun-22, 2deg rows), copied by hand for the cases tested.
CHARITY = {3000: (1.0000, 0.4870, 0.1411), 6500: (1.0000, 0.9436, 0.9621), 9300: (0.6703, 0.7362, 1.0000)}

# The scripted display. `vcp` maps a code to [current, maximum]; D6 is power (1 on, 2 standby).
# Its state goes through `fake_display_state`, whose source the tool embeds ahead of this.
FAKE_DDCUTIL = '''
import json, sys, time
state_path, log_path = {state!r}, {log!r}
started = time.monotonic()
args = sys.argv[sys.argv.index("--noconfig") + 1:]
bus = sys.argv[sys.argv.index("--bus") + 1]
state = read_state(state_path)
def record(**entry):
    with open(log_path, "a") as log:
        log.write(json.dumps({{"bus": bus, "args": args, **entry}}) + "\\n")
record(at="start", time=started)
try:
    if not state["answer"]:
        print("No monitor detected on bus", file=sys.stderr); sys.exit(1)
    if args[0] == "getvcp":
        if args[1].upper() == "D6":
            print("VCP D6 SNC x%02x" % state["power"])
        else:
            current, maximum = state["vcp"][args[1]]
            print("VCP %s C %d %d" % (args[1], current, maximum))
    elif args[0] == "setvcp":
        time.sleep(state["write_seconds"])
        with changing_state(state_path) as state:
            if args[1].upper() == "D6":
                state["power"] = {{"1": 1, "4": 2}}[args[2]]
            elif state["take"]:
                state["vcp"][args[1]][0] = int(args[2])
finally:
    record(at="end", time=time.monotonic())
'''


class Display:
    """One scripted display on HDMI-A-1 (bus 13) and HDMI-A-2 (bus 14), and its ddcutil."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "display.json"
        self.log_path = root / "calls.log"
        self.log_path.touch()
        self.script(vcp={"10": [23, 100], "12": [50, 100]}, power=1, answer=True, take=True, write_seconds=0)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        tool = bin_dir / Path(DDCUTIL).name
        tool.write_text(f"#!{sys.executable} -I\n" + STATE_SOURCE
                        + FAKE_DDCUTIL.format(state=str(self.state_path), log=str(self.log_path)))
        tool.chmod(0o755)
        self.tool = tool
        self.drm = root / "drm"
        for port, bus in ((OUTPUT, 13), ("HDMI-A-2", 14)):
            directory = self.drm / f"card1-{port}"
            directory.mkdir(parents=True)
            (root / f"i2c-{bus}").mkdir()
            (directory / "ddc").symlink_to(root / f"i2c-{bus}")
        self.channel = DdcChannel(self.run, drm_root=self.drm, cache_dir=root / "cache")

    def script(self, **values: object) -> None:
        with changing_state(self.state_path) as state:
            state.update(values)

    def vcp(self, code: str, current: int, maximum: int) -> None:
        with changing_state(self.state_path) as state:
            state["vcp"][code] = [current, maximum]

    def run(self, argv, timeout):
        """The production runner, with the tool's path pointed at the fake."""
        return run_tool([str(self.tool), *argv[1:]], timeout)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def writes(self) -> list[list[str]]:
        """Every setvcp's arguments, in the order they started."""
        return [call["args"][1:] for call in self.calls() if call["at"] == "start" and call["args"][0] == "setvcp"]

    def intervals(self, bus: str) -> list[tuple[float, float]]:
        """(start, end) of every command on `bus`."""
        calls = [call for call in self.calls() if call["bus"] == bus]
        starts = [call["time"] for call in calls if call["at"] == "start"]
        ends = [call["time"] for call in calls if call["at"] == "end"]
        return list(zip(sorted(starts), sorted(ends), strict=True))


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
    """`PictureReports`: keeps every picture part put."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, PictureReport | None]] = []

    def put_picture(self, output_id, picture) -> None:
        self.puts.append((output_id, picture))

    def latest(self, output_id: str = OUTPUT) -> PictureReport | None:
        return next((picture for put, picture in reversed(self.puts) if put == output_id), None)


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


def picture(brightness=UNSET, contrast=UNSET, kelvin=6500, gamma=22) -> OutputPicture:
    return OutputPicture(brightness, contrast, kelvin, gamma)


def document(picture_change: int, part: OutputPicture | None = None, *, change: int = 1,
             output_id: str = OUTPUT) -> OutputDocument:
    always_on = OutputPower(Power.ON, (), "2099-01-01T00:00:00Z", Darkness.DISPLAY_OFF, None)
    return OutputDocument(output_id, change, always_on, BEST_DETECTED, False, False,
                          picture_change=picture_change, picture=picture() if part is None else part)


def on_display(value: int) -> PictureSetting:
    return PictureSetting(ActsOn.DISPLAY, value)


def adjusted(value: int) -> PictureSetting:
    return PictureSetting(ActsOn.PICTURE_ADJUSTMENT, value)


class Running:
    def __init__(self, display: Display) -> None:
        self.display = display
        self.shell = Shell()
        self.reports = Reports()
        self.clock = Clock()
        applied = display.root / "applied"       # the controller's preserved runtime directory
        applied.mkdir(exist_ok=True)
        self.controller = PictureController(DdcPictureAdapter(display.channel), self.shell, self.reports,
                                            FileAppliedPictures(applied), self.clock)

    def connect(self, output_id: str = OUTPUT) -> None:
        self.controller.output(output_id, connected=True, identity=IDENTITY)

    def reported(self, change: int, output_id: str = OUTPUT) -> PictureReport:
        until(lambda: (latest := self.reports.latest(output_id)) is not None and latest.for_change == change)
        return self.reports.latest(output_id)


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


# 1 ---------------------------------------------------------------------------------------------

def test_brightness_on_the_display_is_written_read_back_and_taken(display, running):
    run = running()
    run.controller.document(OUTPUT, document(1, picture(on_display(60))))
    report = run.reported(1)
    assert display.writes() == [["10", "60"]]
    assert report.brightness == SettingReport(ActsOn.DISPLAY, 60, WriteResult.TAKEN)
    assert report.contrast == SettingReport(ActsOn.DISPLAY, 50, None)     # unset: nothing written
    assert (report.brightness_answered, report.contrast_answered) == (True, True)
    assert run.shell.last() == (neutral_operations(OUTPUT, 1), "Brightness: on the display · 60 %")


def test_a_percent_maps_to_the_displays_own_range(display, running):
    display.vcp("10", 50, 255)
    run = running()
    run.controller.document(OUTPUT, document(1, picture(on_display(60))))
    report = run.reported(1)
    assert display.writes() == [["10", "153"]]
    assert report.brightness == SettingReport(ActsOn.DISPLAY, 60, WriteResult.TAKEN)
    assert (vcp_for(60, 255), percent_of(VcpReading(153, 255)), vcp_for(0, 100), vcp_for(100, 100)) == (153, 60, 0, 100)


# 2 ---------------------------------------------------------------------------------------------

def test_a_display_that_reads_back_another_value_did_not_take_it_and_the_setting_stays(display, running):
    display.script(take=False)
    run = running()
    run.controller.document(OUTPUT, document(1, picture(on_display(60))))
    report = run.reported(1)
    assert display.writes() == [["10", "60"]]
    assert report.brightness == SettingReport(ActsOn.DISPLAY, 23, WriteResult.NOT_TAKEN)
    operations, line = run.shell.last()
    assert operations.light_factor == 1.0 and line == "Brightness: on the display · the display did not take it"


def test_a_display_that_does_not_answer_reports_no_answer_and_the_setting_stays(display, running):
    display.script(answer=False)
    run = running()
    run.controller.document(OUTPUT, document(1, picture(on_display(60), on_display(40))))
    report = run.reported(1)
    assert report.brightness == SettingReport(ActsOn.DISPLAY, None, WriteResult.NO_ANSWER)
    assert report.contrast == SettingReport(ActsOn.DISPLAY, None, WriteResult.NO_ANSWER)
    assert (report.brightness_answered, report.contrast_answered) == (False, False)
    assert run.shell.last()[0].light_factor == 1.0 and run.shell.last()[0].contrast_slope == 1.0


# 3 ---------------------------------------------------------------------------------------------

def test_unset_writes_nothing_and_sends_the_neutral_operation(display, running):
    run = running()
    run.controller.document(OUTPUT, document(1, picture()))
    report = run.reported(1)
    assert display.writes() == []
    assert not any(call["args"][0] == "setvcp" for call in display.calls())
    assert run.shell.last() == (neutral_operations(OUTPUT, 1), "Brightness: as the display has it")
    assert report.brightness == SettingReport(ActsOn.DISPLAY, 23, None)


# 4 ---------------------------------------------------------------------------------------------

def test_picture_adjustment_writes_nothing_and_becomes_operations(display, running):
    run = running()
    run.controller.document(OUTPUT, document(1, picture(adjusted(40), adjusted(25), 6500, 24)))
    report = run.reported(1)
    assert display.writes() == []
    operations, line = run.shell.last()
    assert operations == PictureOperations(OUTPUT, 1, (1.0, 1.0, 1.0), 0.8, 0.5, 24 / 22)
    assert line == "Brightness: Photo Wall picture adjustment · 40 %"
    assert report.brightness == report.contrast == SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, None)


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
    operations = operations_for(OUTPUT, 1, picture(kelvin=kelvin))
    assert operations.rgb_multipliers == multipliers
    white = final_pixel((1.0, 1.0, 1.0), operations, inside=True)[:3]
    assert white == pytest.approx([_encoded(value) for value in expected], abs=2e-3)


def test_the_neutral_values_change_nothing_and_the_ranges_hold():
    assert kelvin_multipliers(6500) == (1.0, 1.0, 1.0)
    assert operations_for(OUTPUT, 3, picture(adjusted(50), adjusted(50))) == neutral_operations(OUTPUT, 3)
    assert operations_for(OUTPUT, 3, picture(adjusted(100), adjusted(0), 6500, 18)).light_factor == 2.0
    assert operations_for(OUTPUT, 3, picture(on_display(10), on_display(90))) == neutral_operations(OUTPUT, 3)
    for kelvin in range(3000, 9301, 100):
        assert max(kelvin_multipliers(kelvin)) == 1.0
    for kelvin in (2900, 6550, 9400, 6500.0, True):
        with pytest.raises(ValueError, match="^kelvin$"):
            kelvin_multipliers(kelvin)


def test_the_base_page_says_where_brightness_acts():
    assert brightness_line(on_display(60), 60) == "Brightness: on the display · 60 %"
    assert brightness_line(adjusted(40), None) == "Brightness: Photo Wall picture adjustment · 40 %"
    assert brightness_line(UNSET, 35) == "Brightness: as the display has it"
    assert brightness_line(PictureSetting(ActsOn.PICTURE_ADJUSTMENT, AS_THE_DISPLAY_HAS_IT), None) == (
        "Brightness: as the display has it")
    assert brightness_line(on_display(60), None) == "Brightness: on the display · the display did not take it"
    assert brightness_line(adjusted(40), 80) == "Brightness: Photo Wall picture adjustment · 40 %"   # never a panel value


# 5 ---------------------------------------------------------------------------------------------

def test_newest_wins_a_slow_display_gets_the_first_and_the_last_change_only(display, running):
    display.script(write_seconds=2)
    run = running()
    until(lambda: (latest := run.reports.puts) and latest[-1][1] is None and display.calls())   # appeared
    run.controller.document(OUTPUT, document(2, picture(on_display(20))))
    until(lambda: display.writes() == [["10", "20"]], timeout=5)
    for picture_change in range(3, 7):                  # four more within the first write's 2 s
        run.controller.document(OUTPUT, document(picture_change, picture(on_display(picture_change * 10))))
        time.sleep(0.05)
    assert [call["at"] for call in display.calls() if call["args"][0] == "setvcp"] == ["start"]   # still writing
    report = run.reported(6)
    assert display.writes() == [["10", "20"], ["10", "60"]]
    assert report.brightness == SettingReport(ActsOn.DISPLAY, 60, WriteResult.TAKEN)


# 6 ---------------------------------------------------------------------------------------------

def _overlap(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def test_one_command_at_a_time_per_port_and_ports_do_not_wait_on_each_other(display):
    display.script(write_seconds=0.5)
    power, picture_ddc = DdcCiAdapter(display.channel), DdcPictureAdapter(display.channel)
    calls = [lambda: power.set(OUTPUT, Power.OFF, timeout=10),
             lambda: picture_ddc.write(OUTPUT, "10", 60, maximum=100, timeout=10),
             lambda: picture_ddc.write("HDMI-A-2", "12", 40, maximum=100, timeout=10)]
    threads = [threading.Thread(target=call) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    same_port = display.intervals("13")
    assert len(same_port) == 4                          # D6 write + read-back, 0x10 write + read-back
    assert not any(_overlap(a, b) for index, a in enumerate(same_port) for b in same_port[index + 1:])
    other_port = display.intervals("14")
    assert any(_overlap(a, b) for a in same_port for b in other_port)


def test_a_call_that_waits_past_its_timeout_for_the_port_runs_nothing(display):
    display.script(write_seconds=1.5)
    writing = threading.Thread(target=lambda: display.channel.set(OUTPUT, "10", 60, timeout=10))
    writing.start()
    until(lambda: display.calls(), timeout=5)
    began = time.monotonic()
    assert display.channel.get_continuous(OUTPUT, "10", timeout=0.3) is None
    assert time.monotonic() - began < 1.0
    writing.join(5)
    assert len(display.intervals("13")) == 1


def test_the_channel_reads_continuous_codes_and_refuses_anything_else(display):
    assert display.channel.bus(OUTPUT) == 13 and display.channel.bus("HDMI-A-2") == 14
    assert display.channel.bus("Virtual-1") is None
    assert display.channel.get_continuous(OUTPUT, "10", timeout=5) == VcpReading(23, 100)
    assert display.channel.get_continuous(OUTPUT, "D6", timeout=5) is None   # not a continuous reading
    assert display.channel.get_continuous("Virtual-1", "10", timeout=5) is None
    for current, maximum in ((5, 4), (-1, 10), (1, 0)):
        with pytest.raises(ValueError, match="^vcp_reading$"):
            VcpReading(current, maximum)


# 7 ---------------------------------------------------------------------------------------------

def test_start_and_a_recreated_shell_output_re_send_operations_and_repeat_no_write(display, running):
    run = Running(display)
    run.connect()
    run.connect("HDMI-A-2")
    run.controller.document(OUTPUT, document(4, picture(on_display(60), adjusted(25))))
    run.controller.document("HDMI-A-2", document(2, picture(adjusted(40)), output_id="HDMI-A-2"))
    assert run.shell.sent == []                         # nothing before start
    run.controller.start()
    try:
        run.reported(4)
        run.reported(2, "HDMI-A-2")
        until(lambda: {operations.output_id for operations, _ in run.shell.sent} == {OUTPUT, "HDMI-A-2"})
        assert display.writes() == [["10", "60"]]
        sent = len(run.shell.sent)
        run.controller.shell_output(OUTPUT)
        until(lambda: len(run.shell.sent) == sent + 1)
        operations, line = run.shell.last()
        assert operations == PictureOperations(OUTPUT, 4, (1.0, 1.0, 1.0), 1.0, 0.5, 1.0)
        assert line == "Brightness: on the display · 60 %"
        time.sleep(0.3)
        assert display.writes() == [["10", "60"]]       # the re-send repeats no DDC/CI write
    finally:
        run.controller.stop()


# 8 ---------------------------------------------------------------------------------------------

def test_a_value_changed_on_the_displays_buttons_is_reported_and_not_fought(display, running):
    run = running()
    run.controller.document(OUTPUT, document(1, picture(on_display(60))))
    run.reported(1)
    display.vcp("10", 35, 100)                          # someone pressed the display's buttons
    run.clock.now += REREAD_SECONDS + 1
    until(lambda: run.reports.latest().brightness.read == 35)
    assert run.reports.latest().brightness == SettingReport(ActsOn.DISPLAY, 35, WriteResult.TAKEN)
    assert display.writes() == [["10", "60"]]
    until(lambda: run.shell.last()[1] == "Brightness: on the display · 35 %")
    run.controller.document(OUTPUT, document(2, picture(on_display(60), adjusted(40))))   # the next change
    run.reported(2)
    assert display.writes() == [["10", "60"], ["10", "60"]]


def test_no_re_read_while_nothing_is_set_on_the_display(display, running):
    run = running()
    run.controller.document(OUTPUT, document(1, picture(adjusted(40))))
    run.reported(1)
    reads = len(display.calls())
    run.clock.now += REREAD_SECONDS + 1
    time.sleep(1.5)
    assert len(display.calls()) == reads


# 9 ---------------------------------------------------------------------------------------------

def test_presented_follows_the_shells_echo_and_equal_operations_count_at_once(display, running):
    run = running()
    run.controller.document(OUTPUT, document(6, picture(on_display(60), adjusted(40))))
    assert run.reported(6).presented_change is None
    run.controller.presented(OUTPUT, None)               # a v3 tag: nothing
    run.controller.presented(OUTPUT, 6)
    assert run.reports.latest().presented_change == 6
    run.controller.document(OUTPUT, document(7, picture(on_display(70), adjusted(40))))   # same operations
    until(lambda: run.reports.latest().presented_change == 7)
    run.controller.document(OUTPUT, document(8, picture(on_display(70), adjusted(30))))   # new operations
    report = run.reported(8)
    assert report.presented_change == 7
    run.controller.presented(OUTPUT, 8)
    assert run.reports.latest().presented_change == 8


# 10 --------------------------------------------------------------------------------------------

def test_a_power_change_alone_or_an_older_picture_writes_nothing(display, running):
    """Mutation probe: act on a document whose picture_change did not rise -> the power-only
    document writes 60 over the 35 set on the display's buttons."""
    run = running()
    run.controller.document(OUTPUT, document(3, picture(on_display(60)), change=1))
    run.reported(3)
    sent = len(run.shell.sent)
    display.vcp("10", 35, 100)                          # someone on the display's buttons
    run.controller.document(OUTPUT, document(3, picture(on_display(60)), change=2))   # power change only
    run.controller.document(OUTPUT, document(2, picture(on_display(10)), change=3))   # an older picture
    time.sleep(0.5)
    assert display.writes() == [["10", "60"]] and len(run.shell.sent) == sent


def test_a_restarted_controller_does_not_write_a_change_the_display_already_had(display, running):
    """A Weston restart restarts the controller, which replays the stored document: the change it
    already carried out on this display is only read, so a value set on the display's buttons
    since is left alone until the next picture change. Mutation probe: forget the applied change
    (no `AppliedPictures` check) -> the restart writes 60 over the 35."""
    first = running()
    first.controller.document(OUTPUT, document(3, picture(on_display(60))))
    first.reported(3)
    first.controller.stop()
    display.vcp("10", 35, 100)                          # someone on the display's buttons
    again = running()
    again.controller.document(OUTPUT, document(3, picture(on_display(60))))
    report = again.reported(3)
    assert display.writes() == [["10", "60"]]
    assert report.brightness == SettingReport(ActsOn.DISPLAY, 35, None)
    again.controller.document(OUTPUT, document(4, picture(on_display(70))))
    until(lambda: display.writes() == [["10", "60"], ["10", "70"]])


def test_writes_to_one_display_are_at_least_the_gap_apart(display, running):
    """At most two writes a second per display, however fast the changes come (some displays
    wear with each write). Mutation probe: drop the wait -> two setvcp start closer than the gap."""
    run = running()
    for change, value in enumerate((60, 61, 62, 63), start=1):
        run.controller.document(OUTPUT, document(change, picture(on_display(value), on_display(value - 20))))
        run.reported(change)
    starts = sorted(call["time"] for call in display.calls()
                    if call["at"] == "start" and call["args"][0] == "setvcp")
    assert len(starts) == 8
    assert min(later - earlier for earlier, later in zip(starts, starts[1:])) >= WRITE_GAP_SECONDS - 0.01


def test_another_display_on_the_port_re_applies_and_a_disconnected_port_is_not_written(display, running):
    display.script(write_seconds=0.3)                   # a slow write: the wait below must outlast it
    run = running(connect=False)
    run.controller.output(OUTPUT, connected=False, identity=None)
    run.controller.document(OUTPUT, document(1, picture(on_display(60))))
    run.reported(1)
    assert display.writes() == []                       # applies when the display is connected
    run.controller.output(OUTPUT, connected=True, identity=IDENTITY)
    until(lambda: (latest := run.reports.latest()) is not None
          and latest.brightness.result is WriteResult.TAKEN)   # the write and its read-back are over
    assert display.writes() == [["10", "60"]]
    display.vcp("10", 30, 100)
    run.controller.output(OUTPUT, connected=True, identity=DisplayIdentity("ABC", 1, "Other", None))
    until(lambda: display.writes() == [["10", "60"], ["10", "60"]])


# The report's assembly ---------------------------------------------------------------------------

class Sink:
    def __init__(self) -> None:
        self.puts: list[OutputReport] = []
        self.attempts: list = []

    def put_report(self, report) -> None:
        self.puts.append(report)

    def emit_attempt(self, attempt) -> None:
        self.attempts.append(attempt)


PICTURE_PART = PictureReport(True, True, 1, SettingReport(ActsOn.DISPLAY, 60, WriteResult.TAKEN),
                             SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, None), None)


def _power(connected: bool = True, **changes) -> OutputReport:
    fields = {"output_id": OUTPUT, "connected": connected, "identity": IDENTITY if connected else None,
              "modes": (), "answers": (), "method": None, "for_change": None, "result": None, "in_force": None}
    return OutputReport(**{**fields, **changes})


def test_one_report_carries_the_power_half_and_the_picture_half():
    sink = Sink()
    reports = OutputReports(sink)
    reports.put_picture(OUTPUT, PICTURE_PART)
    assert sink.puts == []                               # held until the first power report
    reports.put_report(_power(picture=None))
    assert sink.puts == [_power(picture=PICTURE_PART)]
    reports.put_report(_power(picture=None))             # unchanged: nothing put
    reports.put_picture(OUTPUT, PICTURE_PART)
    assert len(sink.puts) == 1
    reports.put_report(_power(connected=False))          # no display: no picture part
    assert sink.puts[-1] == _power(connected=False)
    reports.put_report(_power(for_change=2, picture=PictureReport(
        False, False, 9, PICTURE_PART.brightness, PICTURE_PART.contrast, 9)))   # the power half's picture is ignored
    assert sink.puts[-1] == _power(for_change=2, picture=PICTURE_PART)
    reports.emit_attempt("attempt")
    assert sink.attempts == ["attempt"]


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
