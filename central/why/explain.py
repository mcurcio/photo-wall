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
    PRIORITY_RANK,
    PROGRAM_PREFIX,
    Admission,
    Intent,
    OperatorProjection,
    Program,
    RuntimeView,
    RunView,
    Scene,
)
from central.why.model import Beaten, ContentKind, ContentRequest, ContentWhy, PowerWhy
from contracts.node_output import InForceKind, OutputDocument, OutputReport, Power


def content_why(view: RuntimeView, frame_id: str, *, programs: Mapping[str, Program],
                refused: Mapping[str, Admission]) -> ContentWhy:
    """The Frame's content answer at `view.now`. `programs`: the stored Programs by id (for names);
    `refused`: admissions refused by protected Frames whose window covers now, by activation id
    (`kept_out`). Each is beaten "kept-out-by-protected-frames" after the Runs the winner beat,
    the highest rank first."""
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
    kept_out = sorted((Beaten(request=request, because="kept-out-by-protected-frames")
                       for admission in refused.values()
                       if (request := _refused_request(admission, programs)) is not None),
                      key=lambda beaten: (-beaten.request.rank, beaten.request.activation_id))
    if not ordered:
        return ContentWhy(winner=None, beat=tuple(kept_out))
    winner, *losers = ordered
    winning = _request(runs[winner.root_id], programs)
    beat = tuple(Beaten(request=_request(runs[loser.root_id], programs),
                        because="lower-priority" if loser.priority < winner.priority else "admitted-earlier")
                 for loser in losers)
    return ContentWhy(winner=winning, beat=(*beat, *kept_out))


def kept_out(projection: OperatorProjection, programs: Mapping[str, Program], scenes: Mapping[str, Scene],
             frame_id: str) -> dict[str, Admission]:
    """The `refused` `content_why` takes (the kept-out rule above): each Program whose Scene has
    the Frame and whose window covering `projection`'s now was refused by protected Frames, its
    admission by activation id."""
    target = f"frame:{frame_id}"
    refused: dict[str, Admission] = {}
    for program_id, windows in projection.program_windows.items():
        program = programs.get(program_id)
        scene = None if program is None else scenes.get(program.scene_id)
        admission = None if windows.current is None else windows.current.admission
        if (scene is not None and target in scene.participants and admission is not None
                and admission.status == "rejected" and admission.reason == "protected_frames"):
            refused[admission.activation_id] = admission
    return refused


def _refused_request(admission: Admission, programs: Mapping[str, Program]) -> ContentRequest | None:
    """A refused Program window as its request (no Run); None when no stored Program owns it."""
    program = next((program for program in programs.values()
                    if admission.activation_id.startswith(program.window_prefix)), None)
    if program is None:
        return None
    return ContentRequest(kind="program", activation_id=admission.activation_id, run_id=None,
                          scene_id=program.scene_id, program_id=program.program_id, name=program.name,
                          rank=PRIORITY_RANK[program.priority])


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


__all__ = ["content_why", "is_resting", "kept_out", "power_why"]
