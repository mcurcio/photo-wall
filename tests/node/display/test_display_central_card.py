"""Central's card on Display Host's controller (the Frame shows Central's error when it blacks the
Frame; DESIGN-r8 §5): the rule (`central_card`), the facts meeting (`CentralCards`), the adapters
that push each owner's fact (`PowerToCards`, `DrawingToCards`, `tell_cards`), the Player's report as
read off the bus (`BusImpededReports`) and the forwarder to the shell's op (`ShellCards`).

R4, owner 2026-10-20: "A crash in the Player shouldnt have any effect on the ability to
see/detect/recover from it": a Player crash or hang never keeps Central's card up for an ended run.
Identity, never time: the report names the grant the Frame was drawn under, and only the admitted
grant's entry counts.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from appliance.display_host.central_card import CentralCards, OutputFacts, central_card
from contracts.node_impeded import Impeded, ImpededFrames, encode_impeded
from contracts.node_output import CentralCard, InForce, InForceKind, OutputReport, Power
from contracts.room_light import CentralWords

GRANT = "6f1c2a4e-1b2d-4c3e-8f40-5a6b7c8d9e0f"
OTHER_GRANT = "0a1b2c3d-4e5f-4061-8273-8495a6b7c8d9"
WORDS = CentralWords(line1="Photo Wall can't read Family Room · since 26 Oct 15:35",
                     line2="Drawn with its last word: off · see Needs attention")
CARD = CentralCard("HDMI-A-1", WORDS.line1, WORDS.line2)


def report(*, output="HDMI-A-1", frame="frame-1", grant=GRANT, words=WORDS) -> ImpededFrames:
    return ImpededFrames(frames=(Impeded(frame_id=frame, output_id=output, grant_id=grant, lights=("family-room",),
                                         words=words),))


def facts(**changes) -> OutputFacts:
    base = dict(output_id="HDMI-A-1", powered=True, own_drawing=False, admitted_grant=GRANT,
                admitted_frame="frame-1")
    return OutputFacts(**{**base, **changes})


# -- the rule ---------------------------------------------------------------------------------------

def test_the_admitted_runs_report_naming_the_outputs_frame_is_centrals_card_verbatim():
    assert central_card(facts(), report()) == CARD


@pytest.mark.parametrize(("output", "impeded"), [
    (facts(powered=False), report()),                      # 1. Night off or Resting
    (facts(own_drawing=True), report()),                   # 2. a test pattern, target or trial
    (facts(admitted_grant=None, admitted_frame=None), report()),   # 3. the slate: no run admitted
    (facts(), None),                                       # 5. not reported, or it does not decode
    (facts(), ImpededFrames(frames=())),                   # 5. reported, not impeded
    (facts(), report(grant=OTHER_GRANT)),                  # an ended or older run's report
    (facts(), report(frame="frame-2")),                    # another Frame
    (facts(), report(output="HDMI-A-2")),                  # another Output
    (facts(output_id="Virtual-1"), report(output="HDMI-A-1")),   # an Output the wire has no key for
], ids=["powered-off", "own-drawing", "slate", "unreported", "not-impeded", "other-grant", "other-frame",
        "other-output", "unknown-output"])
def test_no_card_otherwise(output, impeded):
    assert central_card(output, impeded) is None


# -- the facts meeting --------------------------------------------------------------------------------

class Reports:
    def __init__(self, latest=None):
        self.value = latest

    def latest(self):
        return self.value


def cards(latest=None):
    sent: list[tuple[str, CentralCard | None]] = []
    reports = Reports(latest)
    return CentralCards(reports, lambda output_id, card: sent.append((output_id, card))), reports, sent


def test_each_owners_fact_composes_the_card_and_only_a_change_is_sent():
    meeting, reports, sent = cards(report())
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=False)
    assert sent == [("HDMI-A-1", CARD)]
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=False)     # unchanged: nothing sent
    meeting.power("HDMI-A-1", True)
    assert sent == [("HDMI-A-1", CARD)]
    meeting.power("HDMI-A-1", False)                                 # Night off
    meeting.power("HDMI-A-1", True)
    meeting.drawing("HDMI-A-1", True)                                # a test pattern
    meeting.drawing("HDMI-A-1", False)
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=True)       # a live trial
    assert sent == [("HDMI-A-1", CARD), ("HDMI-A-1", None), ("HDMI-A-1", CARD), ("HDMI-A-1", None),
                    ("HDMI-A-1", CARD), ("HDMI-A-1", None)]
    assert meeting.card("HDMI-A-1") is None


def test_a_new_report_composes_every_output_again():
    meeting, reports, sent = cards()
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=False)
    meeting.admitted("HDMI-A-2", OTHER_GRANT, "frame-2", trial=False)
    assert sent == [("HDMI-A-1", None), ("HDMI-A-2", None)]
    reports.value = report()
    meeting.reported()
    reports.value = ImpededFrames(frames=())                         # the light came back on
    meeting.reported()
    assert sent[2:] == [("HDMI-A-1", CARD), ("HDMI-A-1", None)]


def test_an_ended_run_never_keeps_the_card_and_a_new_run_must_restate_it():
    """R4: the Player crashed (its admission ends: the slate), then a new run is admitted under a new
    grant while the bus still holds the crashed run's report: no card until the new run restates.
    Mutation probe: compare only frame and Output in `central_card` -> the third send is CARD."""
    meeting, reports, sent = cards(report())
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=False)
    meeting.admitted("HDMI-A-1", None, None, trial=False)              # crashed or hung: the slate
    meeting.admitted("HDMI-A-1", OTHER_GRANT, "frame-1", trial=False)  # the new run
    assert sent == [("HDMI-A-1", CARD), ("HDMI-A-1", None)]
    reports.value = report(grant=OTHER_GRANT)                          # the new run restates
    meeting.reported()
    assert sent[-1] == ("HDMI-A-1", CARD)


# -- the owners' adapters -----------------------------------------------------------------------------

def output_report(power: Power | None) -> OutputReport:
    in_force = None if power is None else InForce(InForceKind.TIMETABLE, power, None, None, None)
    return OutputReport("HDMI-A-1", True, None, (), answers=(), method=None, for_change=None, result=None,
                        in_force=in_force, size_mm=None)


def test_the_power_reports_say_whether_the_display_is_on_and_still_reach_the_bus():
    from appliance.display_host.runner import PowerToCards
    meeting, reports, sent = cards(report())
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=False)
    put = []
    sink = PowerToCards(SimpleNamespace(put_report=put.append, emit_attempt=put.append), meeting)
    for power in (Power.OFF, None, Power.ON):
        sink.put_report(output_report(power))
    assert [card for _, card in sent] == [CARD, None, CARD]
    assert [r.in_force for r in put] == [output_report(p).in_force for p in (Power.OFF, None, Power.ON)]


def test_the_picture_ops_drawing_counts_once_the_shell_took_it():
    from appliance.display_host.runner import DrawingToCards
    meeting, reports, sent = cards(report())
    meeting.admitted("HDMI-A-1", GRANT, "frame-1", trial=False)
    taken = [False, True, True]
    shell = SimpleNamespace(picture=lambda output_id, operations, brightness, *, drawing, timeout: taken.pop(0))
    drawing = DrawingToCards(shell, meeting)
    assert drawing.picture("HDMI-A-1", "{}", "", drawing=True, timeout=1) is False   # refused: nothing
    assert drawing.picture("HDMI-A-1", "{}", "", drawing=True, timeout=1) is True
    assert drawing.picture("HDMI-A-1", "{}", "", drawing=False, timeout=1) is True
    assert [card for _, card in sent] == [CARD, None, CARD]


def _state(*, admitted, diagnostic="released", holding=True, output_id="HDMI-A-1"):
    return SimpleNamespace(key=SimpleNamespace(output_id=output_id), admitted=admitted, diagnostic=diagnostic,
                           holding=holding)


@pytest.mark.parametrize(("state_changes", "granted", "expected"), [
    ({}, True, (GRANT, "frame-1")),
    ({"holding": False}, True, (None, None)),                   # the holding slate is up
    ({"diagnostic": "release_requested"}, True, (None, None)),  # its handoff is not complete
    ({"admitted": None}, True, (None, None)),                   # nothing admitted
    ({}, False, (None, None)),                                  # its grant gone (invalidated)
])
def test_the_admitted_run_is_the_released_held_surface_under_its_grant(state_changes, granted, expected):
    from appliance.display_host.runner import admitted_run, tell_cards
    surface = SimpleNamespace(frame_id="frame-1")
    state = _state(**{"admitted": surface, **state_changes})
    grants = {"HDMI-A-1": (SimpleNamespace(surface=surface), UUID(GRANT))} if granted else {}
    backend = SimpleNamespace(grants=grants, trials={}, host=SimpleNamespace(states=lambda: (state,)))
    assert admitted_run(backend, state) == expected
    meeting, reports, sent = cards(report())
    tell_cards(meeting, backend)
    assert sent == [("HDMI-A-1", CARD if expected[0] else None)]


def test_a_grant_for_another_surface_is_not_the_admitted_runs():
    from appliance.display_host.runner import admitted_run
    state = _state(admitted=SimpleNamespace(frame_id="frame-1"))
    backend = SimpleNamespace(grants={"HDMI-A-1": (SimpleNamespace(surface=object()), uuid4())})
    assert admitted_run(backend, state) == (None, None)


# -- the Player's report off the bus, and the shell's op ----------------------------------------------

def test_the_bus_report_is_the_last_value_that_decodes_and_tells_after_it_is_kept(caplog):
    from appliance.display_host.bus import BusImpededReports
    from contracts.node_impeded import IMPEDED_KEY
    reports = BusImpededReports(url="nats://127.0.0.1:1")        # never started: no connection
    told = []
    reports.on_change(lambda: told.append(reports.latest()))
    reports._reader.state._changed({IMPEDED_KEY: encode_impeded(report())})
    assert reports.latest() == report() and told == [report()]
    reports._reader.state._changed({IMPEDED_KEY: b"{not json"})
    assert reports.latest() is None and told[-1] is None
    assert "does not decode" in caplog.text


def test_the_reader_keeps_only_the_followed_keys():
    from nodeapi.node import StateReader, _Kept
    reader = StateReader("display", "player", frozenset({"impeded"}), url="nats://127.0.0.1:1")
    _Kept(reader.state)._changed({"impeded": b"1", "frames": None})
    assert reader.state.get("impeded") == b"1" and reader.state.get("frames") is None


class CardShell:
    def __init__(self):
        self.sent, self.event = [], threading.Event()

    def central_card(self, output_id, card, *, timeout):
        self.sent.append((output_id, card))
        self.event.set()
        return True


def wait_sent(shell, count):
    for _ in range(200):
        if len(shell.sent) >= count:
            return
        shell.event.wait(0.05)
        shell.event.clear()
    raise AssertionError(shell.sent)


def test_cards_reach_the_shell_newest_once_and_again_when_the_shell_announces_the_output():
    from appliance.display_host.runner import ShellCards
    shell = CardShell()
    forwarder = ShellCards(shell)
    forwarder.shell_output("HDMI-A-1")                           # nothing wanted yet: nothing sent
    forwarder.start()
    try:
        forwarder.document("HDMI-A-1", CARD)
        wait_sent(shell, 1)
        forwarder.document("HDMI-A-1", CARD)                     # unchanged: not sent again
        forwarder.shell_output("HDMI-A-1")                       # a hot-plug: sent again
        wait_sent(shell, 2)
        forwarder.document("HDMI-A-1", None)
        wait_sent(shell, 3)
    finally:
        forwarder.stop()
    assert shell.sent == [("HDMI-A-1", CARD), ("HDMI-A-1", CARD), ("HDMI-A-1", None)]
