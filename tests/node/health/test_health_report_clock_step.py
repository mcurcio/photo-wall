"""The Pi's wall clock stepped by its time client while a card is up: the real judge with a real
raised card, through the real runner. The card's `since` is re-read on the next turn and the health
line is put again, while a steady clock puts nothing more."""
from __future__ import annotations

import datetime
from uuid import uuid4

from appliance.apps.probe import SHIPPED_TIMING, AppRunKey
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.feed import FeedEvent
from appliance.health import runner
from appliance.health.judge import APP_ABSENT, HealthJudge
from appliance.kernel.app_facts import APP_EXITED
from contracts.node_faults import FAULTS
from contracts.node_health import HealthReport

RUN = AppRunKey(uuid4(), 101, 7, 1).document()
OPENED_MS = 1_000
RAISED_MS = OPENED_MS + FAULTS[APP_ABSENT].raise_window_ms + 500
FLOOR_WALL_MS = 1_793_000_000_000            # the build's floor: eleven days behind
STEP_MS = 975_402_000


class _Sink:
    def __init__(self) -> None:
        self.put_reports: list[HealthReport] = []

    def put(self, report: HealthReport) -> None:
        self.put_reports.append(report)


def _raised() -> tuple[runner.HealthRunner, _Sink, list[int], list[int]]:
    """A runner whose judge has raised app_absent on HDMI-A-1, with the first report put."""
    judge = HealthJudge(period_ms=SHIPPED_TIMING.period_ms, miss_limit=SHIPPED_TIMING.miss_limit,
                        startup_ms=SHIPPED_TIMING.startup_ms,
                        pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)
    judge.observe_outputs([{"output_id": "HDMI-A-1", "connected": True, "admitted": RUN,
                            "holding": True, "power": "on"}], 0)
    judge.observe(FeedEvent(0, APP_EXITED, {"run": RUN}, "node"), OPENED_MS)
    boot, wall = [RAISED_MS], [FLOOR_WALL_MS]
    sink = _Sink()
    card = runner.CardClock(wall=lambda: wall[0], zone=lambda: datetime.UTC)
    health = runner.HealthRunner(judge, (), clock=lambda: boot[0], wall=lambda: wall[0], sink=sink,
                                 card=card)
    health.turn()
    assert [report.outputs[0].code for report in sink.put_reports] == [APP_ABSENT]
    return health, sink, boot, wall


def _card_lines(health: runner.HealthRunner, now_ms: int) -> tuple[str, ...]:
    return health.card.instructions(health.judge, now_ms)[0].lines


def test_a_wall_clock_step_moves_the_cards_since_and_puts_the_report_again():
    """Mutation probe: delete the step re-put in HealthRunner._report -> one report."""
    health, sink, boot, wall = _raised()
    before = _card_lines(health, boot[0])
    boot[0] += 500
    wall[0] += 500 + STEP_MS
    health.turn()
    assert len(sink.put_reports) == 2
    assert sink.put_reports[0].outputs[0].since != sink.put_reports[1].outputs[0].since
    assert sink.put_reports[1].outputs[0].code == APP_ABSENT
    assert _card_lines(health, boot[0]) != before


def test_a_steady_clock_with_the_same_card_up_puts_nothing_more():
    health, sink, boot, wall = _raised()
    for _ in range(20):
        boot[0] += 500
        wall[0] += 500
        health.turn()
    assert len(sink.put_reports) == 1


def test_a_small_slew_is_not_a_step():
    health, sink, boot, wall = _raised()
    boot[0] += 500
    wall[0] += 500 + runner.WALL_STEP_MS
    health.turn()
    assert len(sink.put_reports) == 1
