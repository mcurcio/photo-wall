"""The judge's verdict as its Node's bus carries it (issue #126; contracts/node_health.py). Pure: no
I/O and no clock of its own.

Rules this module owns (one home each):
- **Which Output is dark**: one whose verdict shows a card (a display-affecting code), the same
  rule that tints its debug overlay (appliance/health/judge.py `OutputVerdict.card`).
- **When its darkness began**: the card's condition's opening on the Pi's wall clock, read the way
  the card's own time is (judge.py `evidence_wall_s`), as a set time.
"""
from __future__ import annotations

from appliance.health.judge import OutputVerdict, Verdict, evidence_wall_s
from contracts.node_health import HealthReport, OutputHealth
from contracts.node_output import encode_set_time


def _output_health(output: OutputVerdict, now_ms: int, wall_ms: int) -> OutputHealth:
    if output.card is None or output.card_age_ms is None:
        return OutputHealth(output.output, False, None, None)
    since = evidence_wall_s(now_ms - output.card_age_ms, now_ms, wall_ms)
    return OutputHealth(output.output, True, output.card, encode_set_time(since))


def health_report(verdict: Verdict, *, now_ms: int, wall_ms: int, catalogue: str) -> HealthReport:
    """`verdict` as a HealthReport: each connected Output, dark when its card is set, with that
    code and the Pi's wall-clock second its evidence began (`now_ms` the judge's boot clock and
    `wall_ms` the wall clock, read together). ValueError("health_report") or
    ValueError("output_health") when the verdict does not fit the contract (an Output name that is
    not a node token, more than MAX_HEALTH_OUTPUTS Outputs)."""
    return HealthReport(catalogue,
                        tuple(_output_health(output, now_ms, wall_ms) for output in verdict.outputs))


def report_key(verdict: Verdict, now_ms: int) -> tuple:
    """What `health_report(verdict, ...)` says, on the judge's boot clock alone: each Output with
    its card and the boot-clock millisecond its evidence opened (None when it shows no card). Two
    verdicts with the same key give the same report (its `since` is that opening on the wall
    clock), so the runner puts a report only when this key changes, never on a judge transition
    that changes nothing an Output shows."""
    return tuple((output.output, None, None) if output.card is None or output.card_age_ms is None
                 else (output.output, output.card, now_ms - output.card_age_ms)
                 for output in verdict.outputs)


__all__ = ["health_report", "report_key"]
