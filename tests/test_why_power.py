"""Why's power answer, pure (Power and the week; the amended U1): Resting is decided once, in
Central (central/why/explain.py `is_resting`), from what the Pi says it carries out AND its last
attempt's result, so a display that did not answer during Night off reads as a power problem,
never Resting, and the console renders Central's answer only.

Mutation probes: `is_resting` ignoring `result` -> the did-not-answer and another-input cases
read Resting; `power_why` passing no result -> the same through the report."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from support.house_time import UTC_HOUSE

from central.power.timetable import TimedChange, Timetable
from central.why.explain import power_why
from contracts.node_output import (
    Darkness,
    InForce,
    InForceKind,
    OutputReport,
    Power,
    PowerMethod,
    PowerResult,
)

NOW = 1_000_000.0
NIGHT = Timetable(now=Power.OFF, because="night-off",
                  changes=(TimedChange(at=NOW + 3600, power=Power.ON, because="on-underneath"),),
                  until=NOW + 7 * 86400, darkness=Darkness.DISPLAY_OFF)
DOCUMENT = SimpleNamespace(change=4)        # Why reads only the document's change


def report(result: PowerResult | None, *, power: Power = Power.OFF, change: int = 4) -> OutputReport:
    return OutputReport("HDMI-A-1", True, None, (), (PowerMethod.DDC_CI,), PowerMethod.DDC_CI, change,
                        result, InForce(InForceKind.TIMETABLE, power, None, None, "1970-01-12T13:46:40Z"))


def why(answer: OutputReport | None):
    return power_why(timetable=NIGHT, test=None, document=DOCUMENT, report=answer,
                     house=UTC_HOUSE, now=NOW)


@pytest.mark.parametrize("result, resting", [
    (PowerResult.CONFIRMED, True),
    (PowerResult.SIGNAL_STOPPED, True),
    (None, True),                               # already off: no attempt was needed
    (PowerResult.NOT_SUPPORTED, True),          # no method: the Pi shows black instead
    (PowerResult.DID_NOT_ANSWER, False),        # lit, against the wish: a power problem
    (PowerResult.ANOTHER_INPUT, False),         # #118 kept it on
])
def test_resting_needs_an_off_the_pi_carried_out_without_failing(result, resting):
    answer = why(report(result))
    assert answer.wanted is Power.OFF and answer.because == "night-off"
    assert (answer.result, answer.resting) == (result, resting)


def test_a_report_for_an_older_change_or_an_on_is_not_resting():
    stale = why(report(PowerResult.CONFIRMED, change=3))
    assert (stale.pi_says, stale.result, stale.resting) == ("not-heard", None, False)
    assert why(report(PowerResult.CONFIRMED, power=Power.ON)).resting is False
    assert why(None).resting is False
