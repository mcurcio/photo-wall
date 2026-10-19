"""The Pi carries out each Output document through CEC, DDC/CI or signal off (1b D2).

The tools are real executables: fake `ddcutil` and `cec-ctl` scripts that record every argv and
answer from a scripted display (its power, whether it answers each channel, whether the tool
hangs), run through the production `run_tool` (its timeout kills a hung tool). The sysfs and /dev
the adapters map an Output through are a temp tree; signal off goes to a fake compositor; the
controller counts on a fake clock (its monotonic count and its wall clock). Roadmap 2a: a
document's power part is a timetable; these tests keep it "on, no set times" with the console test on
top (the timetable's own crossings are tests/node/display/test_display_power_timetable.py).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from node.display.fake_display_state import SOURCE as STATE_SOURCE
from node.display.fake_display_state import changing_state, read_state

from appliance.display_host.ddc import DdcChannel
from appliance.display_host.output_power import OutputPowerController, in_force, next_deadline
from appliance.display_host.power_methods import (
    CEC_CTL,
    DDCUTIL,
    DdcCiAdapter,
    HdmiCecAdapter,
    SignalOffAdapter,
    run_tool,
)
from contracts.node_output import (
    BEST_DETECTED,
    Darkness,
    DisplayIdentity,
    InForceKind,
    OutputDocument,
    OutputPower,
    Power,
    PowerChange,
    PowerMethod,
    PowerRequest,
    PowerResult,
    encode_set_time,
)

OUTPUT = "HDMI-A-1"
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)   # the spike's monitor

# The scripted display both tools read and write. `power` is the panel's; D6 reads 1 when on and
# 2 in standby (the spike: setvcp D6 4 reads back x02). The state goes through
# `fake_display_state`, whose source each tool embeds ahead of its own.
FAKE_DDCUTIL = '''
import json, sys, time
state_path, log_path = {state!r}, {log!r}
with open(log_path, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
state = read_state(state_path)
if state.get("hang"):
    time.sleep(60)
if not state["ddc"]:
    print("No monitor detected on bus", file=sys.stderr); sys.exit(1)
args = sys.argv[sys.argv.index("--noconfig") + 1:]
if args[0] == "getvcp":
    print("VCP D6 SNC x%02x" % (1 if state["power"] == "on" else 2))
elif args[0] == "setvcp" and args[1] == "D6":
    with changing_state(state_path) as state:
        state["power"] = {{"1": "on", "4": "standby"}}[args[2]]
'''

FAKE_CEC_CTL = '''
import json, sys
state_path, log_path = {state!r}, {log!r}
with open(log_path, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
state = read_state(state_path)
device = sys.argv[sys.argv.index("-d") + 1]
connector = {{"cec0": 35, "cec1": 36}}[device.rsplit("/", 1)[1]]
args = sys.argv[sys.argv.index("-d") + 2:]
print("Driver Info:\\n\\tDRM Connector Info         : card 1, connector %d" % connector)
print("\\tPhysical Address           : 1.0.0.0")
if "--to" in args:
    if not state["cec"]:
        print("\\tTx, Not Acknowledged (4), Max Retries"); sys.exit(0)
    with changing_state(state_path) as state:
        if "--standby" in args:
            state["power"] = "standby"
        if "--image-view-on" in args:
            state["power"] = "on"
    if "--give-device-power-status" in args:
        print("\\tpwr-state: %s (0x0%d)" % (state["power"], 0 if state["power"] == "on" else 1))
'''


class Display:
    """One scripted display on HDMI-A-1 and the tools that reach it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "display.json"
        self.log_path = root / "argv.log"
        self.log_path.touch()
        self.script(power="on", ddc=True, cec=False, hang=False)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for tool, source in ((DDCUTIL, FAKE_DDCUTIL), (CEC_CTL, FAKE_CEC_CTL)):
            path = bin_dir / Path(tool).name
            path.write_text(f"#!{sys.executable} -I\n" + STATE_SOURCE
                            + source.format(state=str(self.state_path), log=str(self.log_path)))
            path.chmod(0o755)
        self.bin = bin_dir
        self.drm = root / "drm"
        for port, bus, connector in ((OUTPUT, 13, 35), ("HDMI-A-2", 14, 36)):
            directory = self.drm / f"card1-{port}"
            directory.mkdir(parents=True)
            (root / f"i2c-{bus}").mkdir()
            (directory / "ddc").symlink_to(root / f"i2c-{bus}")
            (directory / "connector_id").write_text(f"{connector}\n")
        self.dev = root / "dev"
        self.dev.mkdir()
        for name in ("cec0", "cec1", "i2c-13"):
            (self.dev / name).touch()

    def script(self, **values: object) -> None:
        with changing_state(self.state_path) as state:
            state.update(values)

    @property
    def power(self) -> str:
        return read_state(self.state_path)["power"]

    def run(self, argv, timeout):
        """The production runner, with the tool's path pointed at the fake."""
        return run_tool([str(self.bin / Path(argv[0]).name), *argv[1:]], timeout)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls() if "setvcp" in call or "--standby" in call
                or "--image-view-on" in call]


@pytest.fixture
def display(tmp_path):
    subject = Display(tmp_path)
    yield subject
    # Never DDC/CI "hard off" (D6 = 5), in any test.
    for call in subject.calls():
        assert not any(a == "D6" and b in ("5", "05", "x05") for a, b in zip(call, call[1:])), call


class Compositor:
    """`power_methods.OutputPowerControl`: Weston's own output power, as the shell keeps it."""

    def __init__(self, outputs: tuple[str, ...] = (OUTPUT,)) -> None:
        self.powered = {output: Power.ON for output in outputs}
        self.asked: list[tuple[str, Power]] = []

    def output_power(self, output_id, power, *, timeout):
        self.asked.append((output_id, power))
        if output_id not in self.powered:
            return False
        self.powered[output_id] = power
        return True

    def output_powered(self, output_id):
        return self.powered.get(output_id)


WALL_START: Final = 1_791_820_800          # 2026-10-12T16:00:00Z, the Pi's wall clock at the start


class Clock:
    """The Pi's two clocks: `now` the monotonic count, `wall` the wall clock (UTC seconds), both
    moved only by the test. The worker's sleeps are recorded (`waits`, the seconds it asked for)
    and each lasts a moment of real time, so a test steps the worker by its looks (`ticks`), never
    by sleeping."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.wall = float(WALL_START)
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def wait(self, condition, seconds: float) -> None:
        self.waits.append(seconds)
        condition.wait(0.005)

    def ticks(self, count: int = 2, timeout: float = 15.0) -> None:
        """Wait until the worker has slept `count` more times: each sleep follows a look at both
        clocks, so whatever they say now has been seen."""
        start = len(self.waits)
        until(lambda: len(self.waits) >= start + count, timeout)


class Sink:
    def __init__(self) -> None:
        self.reports = []
        self.attempts = []

    def put_report(self, report) -> None:
        self.reports.append(report)

    def emit_attempt(self, attempt) -> None:
        self.attempts.append(attempt)


def ddc(display: Display) -> DdcCiAdapter:
    return DdcCiAdapter(DdcChannel(display.run, drm_root=display.drm, cache_dir=display.root / "cache"))


def cec(display: Display) -> HdmiCecAdapter:
    return HdmiCecAdapter(display.run, dev_root=display.dev, drm_root=display.drm)


def timetable(now: Power = Power.ON, *changes: tuple[float, Power], test: PowerRequest | None = None,
              until: float = WALL_START + 8 * 86400, darkness: Darkness = Darkness.DISPLAY_OFF) -> OutputPower:
    """A power part: `now`, set times (wall-clock seconds, power) and `until`, `test` on top."""
    return OutputPower(now, tuple(PowerChange(encode_set_time(int(at)), power) for at, power in changes),
                       encode_set_time(int(until)), darkness, test)


def document(change: int, test: PowerRequest | None = None, *, power: OutputPower | None = None,
             method=BEST_DETECTED, never_off_on_other_input=False, switch_input_on_power_on=False) -> OutputDocument:
    """A document whose timetable is on with no set times (or `power`), `test` on top."""
    part = timetable(test=test) if power is None else power
    return OutputDocument(OUTPUT, change, part, method, switch_input_on_power_on, never_off_on_other_input, 0)


def console_off(seconds: int = 300, request_id: str = "17") -> PowerRequest:
    return PowerRequest(request_id, Power.OFF, seconds)


class Curtain:
    """output_power.BlackPicture: the shell's black curtain as a fake shell keeps it (`answers`
    False: the shell does not confirm)."""

    def __init__(self, answers: bool = True) -> None:
        self.answers = answers
        self.up: dict[str, bool] = {}
        self.asked: list[tuple[str, bool]] = []

    def show(self, output_id, black, *, timeout):
        self.asked.append((output_id, black))
        if self.answers:
            self.up[output_id] = black
        return self.answers

    def shown(self, output_id):
        return self.up.get(output_id, False)


class Running:
    """A started controller over the given adapters and curtain, stopped at the end."""

    def __init__(self, adapters, clock: Clock | None = None, black: Curtain | None = None) -> None:
        self.sink = Sink()
        self.clock = clock or Clock()
        self.black = black or Curtain()
        self.controller = OutputPowerController({adapter.method: adapter for adapter in adapters},
                                                self.sink, self.clock, self.black)
        self.controller.start()

    def connect(self, connected: bool = True) -> None:
        self.controller.output(OUTPUT, connected=connected, identity=IDENTITY if connected else None,
                               modes=())

    def attempts(self, count: int, timeout: float = 15.0):
        """Wait until `count` attempts were made; then the attempts."""
        until(lambda: len(self.sink.attempts) >= count, timeout)
        return self.sink.attempts

    def looked(self, count: int, timeout: float = 15.0):
        """Wait until the worker has made `count` looks with nothing in force (reports only)."""
        until(lambda: len(self.sink.reports) >= count, timeout)


def until(condition: Callable[[], bool], timeout: float) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


@pytest.fixture
def running():
    started: list[Running] = []

    def start(adapters, clock=None, black=None) -> Running:
        subject = Running(adapters, clock, black)
        started.append(subject)
        return subject

    yield start
    for subject in started:
        subject.controller.stop()


# -- the methods on their tools -------------------------------------------------------------


def test_ddc_off_writes_standby_and_reads_it_back_on_writes_on(display):
    adapter = ddc(display)
    assert adapter.set(OUTPUT, Power.OFF, timeout=10) is PowerResult.CONFIRMED
    assert display.power == "standby"
    calls = display.calls()
    assert calls[0] == ["--bus", "13", "--noconfig", "setvcp", "D6", "4"]
    assert calls[1] == ["--bus", "13", "--noconfig", "getvcp", "D6", "--terse"]
    assert adapter.set(OUTPUT, Power.ON, timeout=10) is PowerResult.CONFIRMED
    assert display.calls()[2] == ["--bus", "13", "--noconfig", "setvcp", "D6", "1"]
    assert display.power == "on"


def test_a_ddc_display_that_does_not_answer_did_not_answer(display):
    display.script(ddc=False)
    adapter = ddc(display)
    assert adapter.probe(OUTPUT, timeout=5) is False
    assert adapter.set(OUTPUT, Power.OFF, timeout=10) is PowerResult.DID_NOT_ANSWER


def test_cec_off_sends_standby_and_reads_power_status_not_acknowledged_did_not_answer(display):
    display.script(cec=True)
    adapter = cec(display)
    assert adapter.set(OUTPUT, Power.OFF, timeout=10) is PowerResult.CONFIRMED
    sent = [call for call in display.calls() if "--to" in call]
    assert sent[0][-3:] == ["--to", "0", "--standby"] and sent[0][:3] == ["-d", str(display.dev / "cec0"), "--playback"]
    assert sent[1][-3:] == ["--to", "0", "--give-device-power-status"]
    display.script(cec=False)
    assert adapter.read(OUTPUT, timeout=5) is None
    assert adapter.set(OUTPUT, Power.ON, timeout=10) is PowerResult.DID_NOT_ANSWER
    adapter.close()
    assert display.calls()[-1] == ["-d", str(display.dev / "cec0"), "--clear"]


def test_signal_off_stops_the_signal_through_the_compositor():
    compositor = Compositor()
    adapter = SignalOffAdapter(compositor)
    assert adapter.probe(OUTPUT, timeout=5) is True
    assert adapter.set(OUTPUT, Power.OFF, timeout=5) is PowerResult.SIGNAL_STOPPED
    assert compositor.asked == [(OUTPUT, Power.OFF)] and adapter.read(OUTPUT, timeout=5) is Power.OFF
    assert adapter.probe("HDMI-A-2", timeout=5) is False


def test_a_hanging_tool_did_not_answer_within_its_timeout(display):
    display.script(hang=True)
    began = time.monotonic()
    assert ddc(display).set(OUTPUT, Power.OFF, timeout=1.0) is PowerResult.DID_NOT_ANSWER
    assert time.monotonic() - began < 3.0


# -- the controller: which method, read before write, the triggers ---------------------------


@pytest.mark.parametrize(("cec_answers", "ddc_answers", "chosen"), [
    (True, True, PowerMethod.HDMI_CEC),
    (False, True, PowerMethod.DDC_CI),
    (False, False, PowerMethod.SIGNAL_OFF),
])
def test_best_detected_takes_cec_over_ddc_over_signal_off(display, running, cec_answers, ddc_answers, chosen):
    display.script(cec=cec_answers, ddc=ddc_answers)
    subject = running([cec(display), ddc(display), SignalOffAdapter(Compositor())])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off()))
    (attempt,) = subject.attempts(1)
    assert attempt.method is chosen
    report = subject.controller.report(OUTPUT)
    assert report.method is chosen and report.for_change == 1
    assert report.answers == tuple(method for method, answers in (
        (PowerMethod.HDMI_CEC, cec_answers), (PowerMethod.DDC_CI, ddc_answers), (PowerMethod.SIGNAL_OFF, True))
        if answers)


def test_when_no_method_answers_the_result_is_not_supported(display, running):
    display.script(ddc=False)
    subject = running([cec(display), ddc(display), SignalOffAdapter(Compositor(outputs=()))])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off()))
    (attempt,) = subject.attempts(1)
    assert attempt.result is PowerResult.NOT_SUPPORTED and attempt.method is None
    assert display.writes() == []


class OtherInput(SignalOffAdapter):
    """Signal off on a display that says (or cannot say) it shows another input (#118)."""

    def __init__(self, compositor, other: bool | None) -> None:
        super().__init__(compositor)
        self.other = other

    def showing_other_input(self, output_id, *, timeout):
        return self.other


@pytest.mark.parametrize(("other", "result", "power"), [
    (True, PowerResult.ANOTHER_INPUT, Power.ON),
    (None, PowerResult.SIGNAL_STOPPED, Power.OFF),
])
def test_never_off_on_another_input_keeps_it_on_and_cannot_tell_goes_ahead(running, other, result, power):
    compositor = Compositor()
    subject = running([OtherInput(compositor, other)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off(), never_off_on_other_input=True))
    (attempt,) = subject.attempts(1)
    assert attempt.result is result and compositor.powered[OUTPUT] is power


def test_a_display_already_off_gets_no_write_and_is_confirmed(display, running):
    display.script(power="standby")
    subject = running([ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off()))
    (attempt,) = subject.attempts(1)
    assert attempt.result is PowerResult.CONFIRMED and display.writes() == []


def test_a_blip_after_a_ddc_standby_writes_nothing(display, running):
    subject = running([ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off()))
    assert subject.attempts(1)[0].result is PowerResult.CONFIRMED
    assert len(display.writes()) == 1 and display.power == "standby"
    subject.connect(False)                       # the standby bounced hot-plug for 250 ms ...
    subject.connect(True)                        # ... and the same display came back
    second = subject.attempts(2)[1]
    assert second.result is PowerResult.CONFIRMED
    assert len(display.writes()) == 1            # read standby: no write, no loop
    assert subject.controller.report(OUTPUT).identity == IDENTITY


class HeldProbe:
    """An adapter whose first probe waits until the test lets it go: a look still probing."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.method = adapter.method
        self.probing = threading.Event()
        self.go = threading.Event()

    def probe(self, output_id, *, timeout):
        if not self.probing.is_set():
            self.probing.set()
            assert self.go.wait(15)
        return self.adapter.probe(output_id, timeout=timeout)

    def __getattr__(self, name):
        return getattr(self.adapter, name)


def test_a_change_that_arrives_while_a_look_probes_is_acted_on_once(display, running):
    held = HeldProbe(ddc(display))
    subject = running([held])
    subject.connect()                            # the start look begins probing ...
    assert held.probing.wait(15)
    subject.controller.document(OUTPUT, document(1, console_off()))   # ... and the document lands
    held.go.set()
    assert subject.attempts(1)[0].in_force.request_id == "17"
    time.sleep(1.5)                              # longer than a worker tick: no second look acts
    assert len(subject.sink.attempts) == 1 and len(display.writes()) == 1


def test_a_console_test_off_ends_on_the_pis_clock_and_the_timetable_comes_back(display, running):
    clock = Clock()
    subject = running([ddc(display)], clock)
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off(300)))
    subject.attempts(1)
    assert display.power == "standby"
    assert subject.controller.report(OUTPUT).in_force.remaining_seconds == 300
    clock.now += 299
    time.sleep(1.5)                              # longer than a worker tick: nothing ends early
    assert len(subject.sink.attempts) == 1
    clock.now += 1                               # no new document: Central is away
    attempt = subject.attempts(2)[1]
    assert (attempt.in_force.kind, attempt.in_force.power, attempt.result) == (
        InForceKind.TIMETABLE, Power.ON, PowerResult.CONFIRMED)
    assert display.power == "on"
    assert subject.controller.report(OUTPUT).in_force.kind is InForceKind.TIMETABLE


def test_a_re_put_of_the_same_request_keeps_its_count(display, running):
    clock = Clock()
    subject = running([ddc(display)], clock)
    subject.connect()
    subject.controller.document(OUTPUT, document(1, console_off(300)))
    subject.attempts(1)
    clock.now += 200
    subject.controller.document(OUTPUT, document(1, console_off(300)))   # a new bus epoch
    subject.controller.document(OUTPUT, document(2, console_off(300)))   # and a change around it
    # The same test and timetable: the change is answered with what is carried out, no new attempt.
    until(lambda: subject.controller.report(OUTPUT).for_change == 2, 15)
    assert len(subject.sink.attempts) == 1
    clock.now += 100                             # the count kept running from the first apply
    assert subject.attempts(2)[1].in_force.kind is InForceKind.TIMETABLE and display.power == "on"


def test_a_restart_with_the_document_present_acts_once(display, running):
    present = document(4, console_off())
    first = running([ddc(display)])
    first.connect()
    first.controller.document(OUTPUT, present)
    first.attempts(1)
    first.controller.stop()
    assert len(display.writes()) == 1
    again = running([ddc(display)])              # the controller restarts with Weston
    again.connect()
    again.controller.document(OUTPUT, present)   # the desired view hands it the document again
    again.controller.document(OUTPUT, present)   # and once more, after a bus epoch
    assert again.attempts(1)[0].result is PowerResult.CONFIRMED
    time.sleep(1.5)
    assert len(again.sink.attempts) == 1 and len(display.writes()) == 1


def test_no_document_leaves_the_display_as_it_is_and_reports_the_methods(display, running):
    subject = running([ddc(display), SignalOffAdapter(Compositor())])
    subject.connect()
    until(lambda: (subject.controller.report(OUTPUT) or None) is not None
          and subject.controller.report(OUTPUT).answers == (PowerMethod.DDC_CI, PowerMethod.SIGNAL_OFF), 15)
    report = subject.controller.report(OUTPUT)
    assert (report.method, report.result, report.in_force, report.for_change) == (None, None, None, None)
    assert display.writes() == [] and subject.sink.attempts == []


def test_in_force_and_next_deadline_follow_the_pis_own_count():
    tested = document(3, console_off(300))
    wall = float(WALL_START)
    until_seconds = 8 * 86400
    assert in_force(None, {}, 0, wall) == in_force(None, {}, 0, wall)
    assert in_force(None, {}, 0, wall).in_force is None and in_force(None, {}, 0, wall).power is None
    assert in_force(tested, {}, 50, wall).in_force.request_id == "17"
    assert in_force(tested, {"17": 10.0}, 309.5, wall).in_force.remaining_seconds == 1
    after = in_force(tested, {"17": 10.0}, 310.0, wall)
    assert (after.in_force.kind, after.power) == (InForceKind.TIMETABLE, Power.ON)
    assert next_deadline(tested, {}, 50, wall) == until_seconds          # not started: only `until`
    assert next_deadline(tested, {"17": 10.0}, 300, wall) == 10.0
    assert next_deadline(document(3), {}, 0, wall) == until_seconds


@pytest.mark.skipif(not os.access("/bin/sh", os.X_OK), reason="needs a POSIX shell")
def test_the_production_runner_runs_tools_on_the_private_cache_and_usr_bin(tmp_path):
    done = run_tool(["/bin/sh", "-c", 'printf "%s %s" "$XDG_CACHE_HOME" "$PATH"'], 5)
    assert done.stdout == "/tmp/ddcutil /usr/bin"
