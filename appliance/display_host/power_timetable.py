"""The Pi's reading of the power timetable (roadmap 2a; Power and the week, slice T). Pure: the
document's power part and the Pi's own wall clock in, what is wanted out. Stdlib and contracts only;
no timezone database and no sun maths on the Pi (Central works out every set time).

Rules this module owns (one home each):
- **Wanted at the Pi's clock:** the power of the last change whose set time the Pi's wall clock has
  reached, else `now` (Central's decision); at or past `until`, nothing is wanted: the display is
  left as it is (InForceKind.TIMETABLE_RAN_OUT).
- **The next look:** the first set time (a change's, or `until`) strictly after the Pi's clock; the
  controller sleeps no longer than that or its tick, so a wall-clock step on the Pi is seen within
  one tick.
- **A document change acts only on a difference:** a new document is carried out at once only
  when what it wants at the Pi's clock differs from what the previous document wanted at the same
  reading (1b's "never between triggers" rule: a daily rewrite never fights a person with the
  remote).
The 1b test on top (counted on the monotonic clock) stays in output_power.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from contracts.node_output import InForceKind, OutputPower, Power, decode_set_time


@dataclass(frozen=True, slots=True)
class Wanted:
    """What the timetable wants at one reading of the Pi's wall clock. `kind` is TIMETABLE or
    TIMETABLE_RAN_OUT; `power` None exactly when ran out; `since` the set time of the change in
    force (None while `now` is)."""
    kind: InForceKind
    power: Power | None
    since: str | None


def wanted(power: OutputPower, wall_now: float) -> Wanted:
    """The timetable's wanted power at the Pi's wall clock `wall_now` (UTC seconds)."""
    if wall_now >= decode_set_time(power.until):
        return Wanted(InForceKind.TIMETABLE_RAN_OUT, None, None)
    reached = [change for change in power.changes if decode_set_time(change.at) <= wall_now]
    if reached:
        return Wanted(InForceKind.TIMETABLE, reached[-1].power, reached[-1].at)
    return Wanted(InForceKind.TIMETABLE, power.now, None)


def next_set_time(power: OutputPower, wall_now: float) -> float | None:
    """The first change's set time, or `until`, strictly after `wall_now`, as UTC seconds; None past
    `until`."""
    moments = [decode_set_time(change.at) for change in power.changes] + [decode_set_time(power.until)]
    return next((float(moment) for moment in moments if moment > wall_now), None)


def acts_on_new_document(previous: OutputPower | None, current: OutputPower, wall_now: float) -> bool:
    """Whether a document whose `change` rose is carried out at once: no previous document, or
    `wanted(previous, wall_now) != wanted(current, wall_now)`, or the test part differs. "Wanted"
    compares what is carried out (kind and power), never `since`: a rewrite that moves `now` past a
    set time already reached wants the same."""
    if previous is None or previous.test != current.test:
        return True
    before, after = wanted(previous, wall_now), wanted(current, wall_now)
    return (before.kind, before.power) != (after.kind, after.power)


__all__ = ["Wanted", "acts_on_new_document", "next_set_time", "wanted"]
