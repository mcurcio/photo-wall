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

from central.displays.model import PowerTest
from central.displays.views import InForceView
from central.house_time.model import HouseTime
from central.power.timetable import Timetable
from central.runtime import Admission, Program, RuntimeView
from central.why.model import ContentWhy, PowerWhy
from contracts.node_output import OutputDocument, OutputReport, Power


def content_why(view: RuntimeView, frame_id: str, *, programs: Mapping[str, Program],
                refused: Mapping[str, Admission]) -> ContentWhy:
    """The Frame's content answer at `view.now`. `programs`: the stored Programs by id (for names);
    `refused`: admissions refused by protected Frames whose window covers now, by activation id
    (empty in slice T)."""
    raise NotImplementedError


def power_why(*, timetable: Timetable | None, test: PowerTest | None, document: OutputDocument | None,
              report: OutputReport | None, house: HouseTime, now: float) -> PowerWhy | None:
    """The Frame's power answer at Central's `now`; None when `timetable` is None (no Output)."""
    raise NotImplementedError


def is_resting(wanted: Power, pi_says: InForceView | Literal["not-heard"]) -> bool:
    """The Resting rule above."""
    raise NotImplementedError


__all__ = ["content_why", "is_resting", "power_why"]
