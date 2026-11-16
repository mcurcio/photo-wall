"""Why a Frame is black while content is wanted, as its Player states it to the health judge.

Pure. The Player's executor gathers the facts (player/executor.py `frames_black`); this is the one
rule that reads them. A Frame draws no statement when it shows something, when the Output is
drawing its own pattern, when a black layer is in force, or when nothing was wanted of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from contracts.node_app_link import FrameBlackCause


@dataclass(frozen=True, slots=True)
class DueLayer:
    """A layer due on the Frame now, as far as the Player has got with it."""

    presentation: Literal["media", "black"]
    order: tuple                # contracts/models.py `stack_order`
    secured: bool
    prepared: bool
    granted: bool
    failure: str | None


def frame_black(*, showing: bool, own_drawing: bool, has_plan: bool, clock_ok: bool,
                due: Sequence[DueLayer], unfilled: bool) -> FrameBlackCause | None:
    """Why this Frame is black, or None when it is not, or is black on purpose. The topmost due
    layer decides; with none due, an unfilled span says a photo was wanted and none placed."""
    if showing or own_drawing:
        return None
    if not has_plan:
        return FrameBlackCause.NO_PLAN
    if not due:
        return FrameBlackCause.NO_CONTENT if unfilled else None
    top = max(due, key=lambda layer: layer.order)
    if top.presentation == "black":
        return None
    if not clock_ok:
        return FrameBlackCause.NO_TIME
    if top.failure in ("download", "integrity") or not top.secured:
        return FrameBlackCause.NO_PHOTO
    if top.failure in ("decode", "capacity") or not top.prepared:
        return FrameBlackCause.CANNOT_DRAW
    return FrameBlackCause.NOT_STARTED if not top.granted else FrameBlackCause.CANNOT_DRAW


__all__ = ["DueLayer", "frame_black"]
