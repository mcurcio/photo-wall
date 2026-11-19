"""A dark Frame's card names why it is dark: three causes that each said "Nothing to show on this
Frame" have their own code and line.

Each through the real pieces with no hardware: Display Host's controller and handoff on a fake shell
(the `outputs` snapshot it serves on every feed read), and the health judge's overlay instruction.
The Player's cause goes the whole way too: its Executor over `UnavailableRenderer`, the
ProbeResponder's statement, the broker's ProbeThread and feed, the health runner's FeedReader.

1. The app broker cannot tell Display its run (`DisplayRuns.tell` fails): Display holds no run, so
   no Output is handed off: `screen_not_told`.
2. The Output document binds no Frame: `no_frame_placed`.
3. The Player's renderer was never made: the Output is offered to its run but never drawn on, and
   the Player says why: `frame_no_renderer`.
"""
from __future__ import annotations

import pytest
from node.display.test_display_handoff import BOOT, BOUND, INCARNATION, Shell
from node.health.test_frame_black_card import FAST, RUN_KEY, WINDOW, Wall

from appliance.apps.display_runs import DisplayRuns
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.display_host.runner import Controller
from appliance.feed import FeedEvent
from appliance.health.judge import HealthJudge
from appliance.kernel.app_facts import APP_RUN_OP, APP_STARTED
from contracts.node_faults import FAULTS
from contracts.node_protocol import OutputKey
from player.rendering import UnavailableRenderer

OUTPUT = "HDMI-A-1"
WALL_MS = 3_600_000         # 01:00 UTC at the judge's 0
SCREEN_NOT_TOLD = "Photos paused — the screen software has not heard from the player"
NO_FRAME_PLACED = "No Frame is placed on this screen"
NO_RENDERER = "Photos paused — the player could not start drawing"


class Screen:
    """Display Host on a fake shell: one connected Output, the controller's handoff settled on
    each read, as the main loop settles it before the health judge reads the feed."""

    def __init__(self) -> None:
        self.shell = Shell()
        self.shell.host.observe_output(OutputKey(BOOT, INCARNATION, OUTPUT, 2, 2), connected=True)
        self.controller = Controller(self.shell)

    def told(self, run=RUN_KEY) -> None:
        """What a delivered `DisplayRuns.tell` carries to Display's ingress."""
        self.controller.receive({"op": APP_RUN_OP, "run": run.document()})

    def placed(self, binding) -> None:
        """The Output document's binding (None: it binds no Frame)."""
        self.controller.handoffs.document(OUTPUT, binding)

    def snapshot(self) -> list[dict]:
        self.controller.handoffs.settle(self.shell.host)
        return self.controller.outputs_snapshot()


def judge() -> HealthJudge:
    subject = HealthJudge(period_ms=FAST.period_ms, miss_limit=FAST.miss_limit, startup_ms=FAST.startup_ms,
                          pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)
    subject.observe(FeedEvent(0, APP_STARTED, {"run": RUN_KEY.document()}, "node"), 0)
    return subject


def card(subject: HealthJudge, now: int) -> tuple[str, str] | None:
    """The Output's card, its time left out: (household line, "{code} · Output {output}")."""
    (instruction,) = subject.instructions(now, wall_ms=WALL_MS + now)
    if not instruction.tint:
        return None
    return instruction.lines[0].split(" · since ")[0], instruction.lines[1]


def test_a_broker_that_cannot_tell_display_its_run_says_the_screen_has_not_heard(tmp_path):
    """Mutation probes: `not_handed_off` names no_frame for a missing run; the judge maps both
    reasons to one code."""
    screen = Screen()
    screen.placed(BOUND)
    assert DisplayRuns(tmp_path / "ingress.sock").tell(RUN_KEY) is False      # no ingress: never told
    subject = judge()
    subject.observe_outputs(screen.snapshot(), 0)
    assert card(subject, WINDOW - 1) is None                                  # a cold start's window
    assert card(subject, WINDOW) == (SCREEN_NOT_TOLD, f"screen_not_told · Output {OUTPUT}")
    screen.told()                                                              # told at last: offered
    subject.observe_outputs(screen.snapshot(), WINDOW + 1)
    assert card(subject, WINDOW + 1) == ("Nothing to show on this Frame", f"nothing_to_show · Output {OUTPUT}")


def test_an_output_with_no_frame_placed_says_so():
    screen = Screen()
    screen.told()
    screen.placed(None)
    subject = judge()
    subject.observe_outputs(screen.snapshot(), 0)
    assert card(subject, WINDOW - 1) is None
    assert card(subject, WINDOW) == (NO_FRAME_PLACED, f"no_frame_placed · Output {OUTPUT}")
    screen.placed(BOUND)                                                       # a Frame placed: offered
    subject.observe_outputs(screen.snapshot(), WINDOW + 1)
    assert card(subject, WINDOW + 1)[1] == f"nothing_to_show · Output {OUTPUT}"


@pytest.fixture
def unrendered(tmp_path):
    """A configured Player with no plan whose renderer could not be made, probed by the broker."""
    wall = Wall(tmp_path, None, renderer=UnavailableRenderer(), output=OUTPUT)
    yield wall
    wall.close()


def test_a_player_whose_renderer_was_never_made_says_it_could_not_start_drawing(unrendered):
    """The Output is offered to the run and never drawn on: the Player's own statement names it.
    Mutation probes: drop the renderer fact from `frame_black`; count only an admitted run's
    report; put `nothing_to_show` before the Frame-black codes."""
    screen = Screen()
    screen.told()
    screen.placed(BOUND)
    wall = unrendered
    wall.judge.observe_outputs(screen.snapshot(), wall.now_ms)
    wall.exchange()
    assert screen.shell.host.state(OUTPUT).admitted is None                   # offered, never drawn
    (instruction,) = wall.card(WINDOW)
    assert instruction.tint
    assert (instruction.lines[0].split(" · since ")[0], instruction.lines[1]) == (
        NO_RENDERER, f"frame_no_renderer · Output {OUTPUT}")
