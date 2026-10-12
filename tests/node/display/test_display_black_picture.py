"""Black picture and the black fallback on the Pi (roadmap 2a, #83; Power and the week, slice S2):
"a node test that an off target runs the display's chosen method".

The controller runs over the real DDC/CI and CEC adapters on the fake `ddcutil` and `cec-ctl`
harness (tests/node/display/test_display_power_methods.py) and a fake shell curtain; the backend's
`black` op is then played over a real SEQPACKET pair, the far end speaking the shell's protocol
(Linux). The shell's own curtain is proven in the display harness (tests/native_display_smoke.py).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from uuid import uuid4

import pytest
from node.display.test_display_power_methods import (
    OUTPUT,
    WALL_START,
    Clock,
    Compositor,
    Curtain,
    Display,
    Running,
    cec,
    console_off,
    ddc,
    document,
    timetable,
)

from appliance.display_host.power_methods import SignalOffAdapter
from contracts.node_output import Darkness, InForceKind, Power, PowerMethod, PowerResult

LINUX = sys.platform.startswith("linux")
OFF_THEN_ON = ((WALL_START + 60, Power.ON),)


@pytest.fixture
def display(tmp_path):
    return Display(tmp_path)


@pytest.fixture
def running():
    started: list[Running] = []

    def start(adapters, *, black: Curtain | None = None) -> Running:
        subject = Running(adapters, Clock(), black)
        started.append(subject)
        return subject

    yield start
    for subject in started:
        subject.controller.stop()


def test_black_picture_off_shows_black_and_runs_no_power_method_and_its_on_takes_it_down(display, running):
    """Mutation probe (P-black-as-off): run the power method for black picture -> the "no power
    write" assertion fails."""
    subject = running([cec(display), ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(
        Power.OFF, *OFF_THEN_ON, darkness=Darkness.BLACK_PICTURE)))
    (off,) = subject.attempts(1)
    assert (off.in_force.kind, off.in_force.power, off.result) == (
        InForceKind.TIMETABLE, Power.OFF, PowerResult.CONFIRMED)
    assert subject.black.up == {OUTPUT: True}
    assert display.writes() == [] and display.power == "on"      # the display stays on

    subject.clock.wall = WALL_START + 61                          # the set time: on
    _, on = subject.attempts(2)
    assert (on.in_force.power, on.result) == (Power.ON, PowerResult.CONFIRMED)
    assert subject.black.up == {OUTPUT: False} and display.writes() == []


def test_black_picture_the_shell_does_not_confirm_did_not_answer(display, running):
    subject = running([ddc(display)], black=Curtain(answers=False))
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(Power.OFF, darkness=Darkness.BLACK_PICTURE)))
    (attempt,) = subject.attempts(1)
    assert attempt.result is PowerResult.DID_NOT_ANSWER and display.writes() == []


def test_display_off_with_no_answering_method_shows_black_and_reports_not_supported(display, running):
    display.script(ddc=False)
    subject = running([cec(display), ddc(display), SignalOffAdapter(Compositor(outputs=()))])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(Power.OFF, *OFF_THEN_ON)))
    (off,) = subject.attempts(1)
    assert (off.result, off.method) == (PowerResult.NOT_SUPPORTED, None)
    assert subject.controller.report(OUTPUT).result is PowerResult.NOT_SUPPORTED    # the Power tab's answer
    assert subject.black.up == {OUTPUT: True} and display.writes() == []

    subject.clock.wall = WALL_START + 61
    _, on = subject.attempts(2)
    assert on.in_force.power is Power.ON and subject.black.up == {OUTPUT: False}


def test_black_pictures_on_after_a_test_off_brings_the_display_itself_back_on(display, running):
    """The 1b test turns the display off through its method; when it ends, black picture's "on"
    must bring the display on too, not only lower the curtain, and the result describes the
    display.

    Mutation probe: black picture's on lowers the curtain only -> the display stays in standby."""
    subject = running([ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(
        Power.ON, test=console_off(300), darkness=Darkness.BLACK_PICTURE)))
    subject.attempts(1)
    assert display.power == "standby"
    subject.clock.now += 301                                     # the test ends on the Pi's count
    _, on = subject.attempts(2)
    assert (on.in_force.kind, on.in_force.power, on.method, on.result) == (
        InForceKind.TIMETABLE, Power.ON, PowerMethod.DDC_CI, PowerResult.CONFIRMED)
    assert display.power == "on" and subject.black.up.get(OUTPUT) is not True


def test_switching_to_black_picture_during_night_off_is_acted_on_and_the_morning_turns_it_on(
        display, running):
    """A darkness change is a change the Pi acts on (`acts_on_new_document`), and the morning's on
    brings back a display the night had turned off.

    Mutation probe: `acts_on_new_document` comparing kind and power only -> no second attempt."""
    subject = running([ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(Power.OFF, *OFF_THEN_ON)))
    subject.attempts(1)
    assert display.power == "standby"
    subject.controller.document(OUTPUT, document(2, power=timetable(
        Power.OFF, *OFF_THEN_ON, darkness=Darkness.BLACK_PICTURE)))
    _, black = subject.attempts(2)
    assert (black.for_change, black.result) == (2, PowerResult.CONFIRMED)
    assert subject.black.up == {OUTPUT: True}
    subject.clock.wall = WALL_START + 61                          # the set time: on
    _, _, on = subject.attempts(3)
    assert (on.in_force.power, on.result) == (Power.ON, PowerResult.CONFIRMED)
    assert display.power == "on" and subject.black.up == {OUTPUT: False}


def test_an_on_through_a_method_takes_the_black_curtain_down_first(display, running):
    """A display-off night on a display no method answered shows black; when a method answers at
    the morning's on, the display powers on AND the curtain comes down.

    Mutation probe: no curtain-down in the method branch -> the picture stays black."""
    display.script(ddc=False)
    subject = running([ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(Power.OFF, *OFF_THEN_ON)))
    (off,) = subject.attempts(1)
    assert off.result is PowerResult.NOT_SUPPORTED and subject.black.up == {OUTPUT: True}
    display.script(ddc=True)
    subject.clock.wall = WALL_START + 61
    _, on = subject.attempts(2)
    assert (on.method, on.result) == (PowerMethod.DDC_CI, PowerResult.CONFIRMED)
    assert subject.black.up == {OUTPUT: False}


def test_display_off_with_ddc_runs_ddc_and_shows_no_black(display, running):
    subject = running([cec(display), ddc(display)])
    subject.connect()
    subject.controller.document(OUTPUT, document(1, power=timetable(Power.OFF)))
    (attempt,) = subject.attempts(1)
    assert (attempt.method, attempt.result) == (PowerMethod.DDC_CI, PowerResult.CONFIRMED)
    assert display.power == "standby" and display.writes() == [["--bus", "13", "--noconfig", "setvcp", "D6", "4"]]
    assert (OUTPUT, True) not in subject.black.asked


@pytest.mark.skipif(not LINUX, reason="the shell's control socket is SOCK_SEQPACKET with SO_PEERCRED (Linux)")
def test_the_backend_puts_the_shells_black_curtain_up_and_down():
    """The runner's BlackPicture over WestonBackend on a real SEQPACKET pair, the far end playing
    the shell: `black` sends the op and is confirmed by the shell's `black` event; an Output the
    shell no longer has is no curtain."""
    from appliance.display_host.runner import ShellBlackPicture
    from appliance.display_host.weston import WestonBackend

    ours, shell = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    boot, incarnation = uuid4(), uuid4()
    output = {"kernel_boot_id": str(boot), "display_host_incarnation": str(incarnation),
              "output_id": OUTPUT, "connection_generation": 1, "mode_generation": 1}

    def send(value):
        shell.send(json.dumps(value).encode())

    send({"event": "hello", "protocol": 1, "boot_id": str(boot), "incarnation_id": str(incarnation)})
    send({"event": "output", "output": output, "connected": True, "width": 640, "height": 480,
          "power": "on", "black": False})
    asked = []

    def play_shell(times: int) -> None:
        for _ in range(times):
            request = json.loads(shell.recv(16384))
            asked.append(request)
            send({"event": "black", "output_id": request["output_id"], "on": request["on"]})
            send({"event": "response", "request_id": request["request_id"], "accepted": True})

    with ours, shell:
        backend = WestonBackend(ours, display_uid=os.getuid(), compositor_pid=os.getpid(),
                                verify_process=lambda grant: True)
        backend.initialize()
        black = ShellBlackPicture(backend)
        assert black.shown(OUTPUT) is None and not black.show(OUTPUT, True, timeout=1)   # not seen yet
        backend.dispatch()
        assert black.shown(OUTPUT) is False
        playing = threading.Thread(target=play_shell, args=(2,))
        playing.start()
        assert black.show(OUTPUT, True, timeout=2) and black.shown(OUTPUT) is True
        assert black.show(OUTPUT, False, timeout=2) and black.shown(OUTPUT) is False
        playing.join(5)
        assert [(request["op"], request["on"], request["output"]) for request in asked] == [
            ("black", True, output), ("black", False, output)]
        send({"event": "output", "output": {**output, "connection_generation": 2, "mode_generation": 2},
              "connected": False, "width": 0, "height": 0, "power": "on", "black": False})
        backend.dispatch()
        assert black.shown(OUTPUT) is None
