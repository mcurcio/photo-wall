"""A Frame's Why, pure (Power and the week: slice T, extended by slice S3 for "kept out").

Rules this module owns (one home each):
- **The winner** is the Frame's visible Intent in Runtime's view (`RuntimeView.for_target`); each
  other root Run contributing to the Frame is beaten "lower-priority" when its rank is lower, else
  "admitted-earlier" (Runtime's tie rule: the request admitted last wins). A child Run speaks for
  its root.
- **Kept out** (slice S3): an admission refused with reason "protected_frames" whose Scene has the
  Frame, for a request whose window covers now, is beaten "kept-out-by-protected-frames".
- **Kinds**: an activation id starting "program:" is a Program's window, "default:" the Default
  Scene, anything else Show now.
- **Power**: a live 1b test wins ("test"); else the timetable's reason at now. `pi_says` is the
  report's in-force for the latest document change, else "not-heard".
- **Resting** (the amended U1): wanted off and the Pi carries out an off from the test or the
  timetable. A display that did not answer is a power problem, not Resting.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from central.displays.model import PowerTest, live_test
from central.displays.views import InForceView, in_force_view
from central.house_time.calculator import local_date_time
from central.house_time.model import HouseTime
from central.power.timetable import Timetable
from central.runtime import (
    DEFAULT_PREFIX,
    PROGRAM_PREFIX,
    Admission,
    Intent,
    Program,
    RuntimeView,
    RunView,
)
from central.why.model import Beaten, ContentKind, ContentRequest, ContentWhy, PowerWhy
from contracts.node_output import InForceKind, OutputDocument, OutputReport, Power


def content_why(view: RuntimeView, frame_id: str, *, programs: Mapping[str, Program],
                refused: Mapping[str, Admission]) -> ContentWhy:
    """The Frame's content answer at `view.now`. `programs`: the stored Programs by id (for names);
    `refused`: admissions refused by protected Frames whose window covers now, by activation id
    (empty in slice T)."""
    target = f"frame:{frame_id}"
    runs = {run.run_id: run for run in view.runs}
    # Each root Run on the Frame, at its strongest contribution there (a child speaks for its root).
    strongest: dict[str, Intent] = {}
    for intent in view.contributions:
        if intent.target != target:
            continue
        held = strongest.get(intent.root_id)
        if held is None or intent.precedence > held.precedence:
            strongest[intent.root_id] = intent
    ordered = sorted(strongest.values(), key=lambda intent: intent.precedence, reverse=True)
    if not ordered:
        return ContentWhy(winner=None, beat=())
    winner, *losers = ordered
    winning = _request(runs[winner.root_id], programs)
    beat = tuple(Beaten(request=_request(runs[loser.root_id], programs),
                        because="lower-priority" if loser.priority < winner.priority else "admitted-earlier")
                 for loser in losers)
    return ContentWhy(winner=winning, beat=beat)


def _request(root: RunView, programs: Mapping[str, Program]) -> ContentRequest:
    """A root Run as the request that admitted it (the kinds rule above)."""
    activation_id = root.activation_id or root.run_id
    kind: ContentKind = ("program" if activation_id.startswith(PROGRAM_PREFIX)
                         else "default-scene" if activation_id.startswith(DEFAULT_PREFIX) else "show-now")
    program = programs.get(root.program_id) if kind == "program" and root.program_id else None
    return ContentRequest(kind=kind, activation_id=activation_id, run_id=root.run_id, scene_id=root.scene_id,
                          program_id=root.program_id if kind == "program" else None,
                          name=program.name if program is not None else root.scene_id, rank=root.priority)


def power_why(*, timetable: Timetable | None, test: PowerTest | None, document: OutputDocument | None,
              report: OutputReport | None, house: HouseTime, now: float) -> PowerWhy | None:
    """The Frame's power answer at Central's `now`; None when `timetable` is None (no Output)."""
    if timetable is None:
        return None
    live = live_test(test, now)
    if live is not None:
        wanted, because, until = live.power, "test", live.ends_at
    else:
        wanted, because = timetable.now, timetable.because
        following = timetable.next_change_after(now)
        until = None if following is None else following.at
    answered = (document is not None and report is not None and report.in_force is not None
                and report.for_change == document.change)
    pi_says = in_force_view(report, house, live, now) if answered else None
    said: InForceView | Literal["not-heard"] = "not-heard" if pi_says is None else pi_says
    return PowerWhy(wanted=wanted, because=because,
                    until=None if until is None else local_date_time(house, until),
                    pi_says=said, resting=is_resting(wanted, said))


def is_resting(wanted: Power, pi_says: InForceView | Literal["not-heard"]) -> bool:
    """The Resting rule above."""
    return (wanted is Power.OFF and pi_says != "not-heard"
            and pi_says.kind in (InForceKind.TEST, InForceKind.TIMETABLE) and pi_says.power is Power.OFF)


__all__ = ["content_why", "is_resting", "power_why"]
