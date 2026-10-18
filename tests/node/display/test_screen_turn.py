"""Display Host turns each screen as its Output document says (render redesign S3, bead B1;
appliance/display_host/screen_turn.py): the wanted turn is the newest document's, asked of the shell
once per trigger (a document whose turn the shell has not confirmed, or the Output appearing
connected) on the worker's own thread, never in a loop, the newest document winning. The unit cases
drive `ScreenTurns` over a fake shell; the Linux case drives it over a real `WestonBackend` on a
SEQPACKET pair whose far end plays the shell (tests/node/display/test_display_turn.py's `Shell`).
"""
from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from node.display.test_display_turn import LINUX, OUTPUT, TRANSFORMS, Shell, confirmed

from appliance.display_host import screen_turn
from appliance.display_host.screen_turn import TURN_SECONDS, ScreenTurns, turn_due
from contracts.display_turn import DisplayTurn
from contracts.node_output import BEST_DETECTED, Darkness, OutputDocument, OutputPower, Power

OTHER = "HDMI-A-2"          # the sentinel Output: its ask proves the worker passed everything before


def document(turn: DisplayTurn, *, output_id: str = OUTPUT, change: int = 1) -> OutputDocument:
    return OutputDocument(output_id, change,
                          OutputPower(Power.ON, (), "2099-01-01T00:00:00Z", Darkness.DISPLAY_OFF, None),
                          BEST_DETECTED, False, False, turn)


def until(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the worker did not get there"
        time.sleep(0.005)


class FakeShell:
    """The shell's `turn` op: answers `connected`'s membership (an Output the shell has), records
    every ask, and holds an ask while `gate` is clear."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, DisplayTurn]] = []
        self.connected: set[str] = {OUTPUT, OTHER}
        self.refuse = False
        self.gate = threading.Event()
        self.gate.set()
        self.waiting = threading.Event()

    def turn(self, output_id: str, turn: DisplayTurn, *, timeout: float) -> bool:
        assert timeout == TURN_SECONDS
        self.asked.append((output_id, turn))
        self.waiting.set()
        self.gate.wait(5)
        return not self.refuse and output_id in self.connected

    def mine(self) -> list[DisplayTurn]:
        return [turn for output_id, turn in self.asked if output_id == OUTPUT]


@pytest.fixture
def turns() -> Iterator[tuple[ScreenTurns, FakeShell]]:
    shell = FakeShell()
    subject = ScreenTurns(shell)
    subject.start()
    try:
        yield subject, shell
    finally:
        shell.gate.set()
        subject.stop()


def settle(subject: ScreenTurns, shell: FakeShell) -> None:
    """Wait until the worker has carried out every ask queued so far: a sentinel ask on another
    Output, queued last, is carried out after them (one worker, asks in the order they came)."""
    sentinel = len(shell.asked)
    shell.connected.add(OTHER)
    subject.output(OTHER, connected=False)                # forgets OTHER's confirmed turn: 90 is due
    subject.document(OTHER, document(90, output_id=OTHER))
    until(lambda: any(output_id == OTHER for output_id, _turn in shell.asked[sentinel:]))


@pytest.mark.parametrize(("wanted", "confirmed_turn", "appeared", "due"), [
    (None, None, False, False), (None, None, True, False), (None, 90, True, False),
    (90, None, False, True), (90, 90, False, False), (90, 0, False, True), (0, None, False, True),
    (90, 90, True, True), (90, None, True, True), (0, 0, True, True), (0, 90, False, True),
])
def test_turn_due_asks_for_a_wanted_turn_not_confirmed_or_on_the_output_appearing(
        wanted, confirmed_turn, appeared, due):
    assert turn_due(wanted, confirmed_turn, appeared=appeared) is due


def test_a_document_asks_once_for_its_turn_and_the_same_turn_again_asks_nothing(turns):
    subject, shell = turns
    subject.document(OUTPUT, document(90))
    until(lambda: subject.confirmed(OUTPUT) == 90)
    assert shell.mine() == [90]
    subject.document(OUTPUT, document(90, change=2))     # a `change` rise with the same turn
    settle(subject, shell)
    assert shell.mine() == [90]
    subject.document(OUTPUT, document(0, change=3))
    until(lambda: subject.confirmed(OUTPUT) == 0)
    assert shell.mine() == [90, 0]


def test_a_refused_ask_on_a_connected_output_is_asked_again_until_confirmed(monkeypatch):
    """Documents are sent only when they change: a turn the shell refused (busy remoding at boot) is
    asked again with a growing wait while the Output is connected, with no new trigger, until the
    shell confirms it (render planes end review)."""
    monkeypatch.setattr(screen_turn, "RETRY_FIRST", 0.02)
    shell = FakeShell()
    subject = ScreenTurns(shell)
    subject.start()
    try:
        shell.refuse = True
        subject.output(OUTPUT, connected=True)
        subject.document(OUTPUT, document(270))
        until(lambda: len(shell.mine()) >= 3)              # asked again with no trigger, more than once
        assert subject.confirmed(OUTPUT) is None and set(shell.mine()) == {270}
        shell.refuse = False
        until(lambda: subject.confirmed(OUTPUT) == 270)
        asked = len(shell.mine())
        time.sleep(0.2)
        assert len(shell.mine()) == asked                    # confirmed: nothing asked after it
    finally:
        shell.gate.set()
        subject.stop()


def test_a_refused_ask_of_an_output_never_reported_connected_waits_for_the_next_trigger(turns):
    subject, shell = turns
    shell.refuse = True
    subject.document(OUTPUT, document(270))
    settle(subject, shell)
    settle(subject, shell)
    assert shell.mine() == [270] and subject.confirmed(OUTPUT) is None
    shell.refuse = False
    subject.document(OUTPUT, document(270, change=2))    # the next trigger: still not confirmed
    until(lambda: subject.confirmed(OUTPUT) == 270)
    assert shell.mine() == [270, 270]


def test_a_document_before_the_output_is_asked_again_when_the_output_appears(turns):
    """At start the documents arrive before the shell has announced any Output: the ask is refused,
    and the Output's first connected report asks again."""
    subject, shell = turns
    shell.connected.discard(OUTPUT)
    subject.document(OUTPUT, document(90))
    settle(subject, shell)
    assert shell.mine() == [90] and subject.confirmed(OUTPUT) is None
    shell.connected.add(OUTPUT)
    subject.output(OUTPUT, connected=True)
    until(lambda: subject.confirmed(OUTPUT) == 90)
    assert shell.mine() == [90, 90]


def test_the_output_appearing_again_asks_for_its_turn_again_and_a_disconnect_forgets_it(turns):
    """A hot-plug (the Output reported connected under a new key) asks again for the wanted turn,
    even the one confirmed before; a disconnect forgets the confirmed turn and asks nothing."""
    subject, shell = turns
    subject.document(OUTPUT, document(180))
    until(lambda: subject.confirmed(OUTPUT) == 180)
    subject.output(OUTPUT, connected=True)
    until(lambda: len(shell.mine()) == 2)
    assert shell.mine() == [180, 180]
    until(lambda: subject.confirmed(OUTPUT) == 180)
    subject.output(OUTPUT, connected=False)
    assert subject.confirmed(OUTPUT) is None
    settle(subject, shell)
    assert shell.mine() == [180, 180]


def test_an_output_appearing_with_no_document_asks_nothing(turns):
    subject, shell = turns
    subject.output(OUTPUT, connected=True)
    settle(subject, shell)
    assert shell.mine() == [] and subject.confirmed(OUTPUT) is None


def test_the_newest_document_replaces_the_one_waiting_while_the_worker_asks(turns):
    subject, shell = turns
    shell.gate.clear()
    subject.document(OUTPUT, document(90))
    assert shell.waiting.wait(5)                          # the worker is held in the shell's turn
    started = time.monotonic()
    subject.document(OUTPUT, document(180, change=2))
    subject.document(OUTPUT, document(270, change=3))
    subject.output(OUTPUT, connected=True)
    assert time.monotonic() - started < 0.5               # neither caller waits on the shell
    shell.gate.set()
    until(lambda: subject.confirmed(OUTPUT) == 270)
    settle(subject, shell)
    assert shell.mine() == [90, 270]


def test_a_document_back_to_the_confirmed_turn_drops_the_waiting_ask(turns):
    subject, shell = turns
    subject.document(OUTPUT, document(90))
    until(lambda: subject.confirmed(OUTPUT) == 90)
    shell.gate.clear()
    subject.document(OTHER, document(0, output_id=OTHER))
    assert shell.waiting.wait(5)                          # held on the other Output
    subject.document(OUTPUT, document(180, change=2))
    subject.document(OUTPUT, document(90, change=3))      # back to what the shell has
    shell.gate.set()
    settle(subject, shell)
    assert shell.mine() == [90] and subject.confirmed(OUTPUT) == 90


@pytest.mark.skipif(not LINUX, reason="the shell's control socket is SOCK_SEQPACKET with SO_PEERCRED (Linux)")
def test_screen_turns_over_the_real_backend_sends_the_turn_op_and_confirms_on_the_turn_event():
    from appliance.display_host.weston import WestonBackend

    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with ours, theirs:
        shell = Shell(theirs)
        backend = WestonBackend(ours, display_uid=os.getuid(), compositor_pid=os.getpid(),
                                verify_process=lambda grant: True)
        backend.initialize()
        backend.dispatch()                                # the shell's Output
        playing = shell.play(1, confirmed)
        subject = ScreenTurns(backend)
        subject.start()
        try:
            subject.document(OUTPUT, document(90))
            until(lambda: subject.confirmed(OUTPUT) == 90)
        finally:
            subject.stop()
        playing.join(5)
        assert [(r["op"], r["output_id"], r["output"], r["transform"]) for r in shell.asked] == [
            ("turn", OUTPUT, shell.output, TRANSFORMS[90])]
