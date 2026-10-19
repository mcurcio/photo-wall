"""Frames as the photo fit rule sees them, for pure tests (frame box run fb-8): a Frame's
`FrameOutput` (its Frame output, "Shows videos" and physical size) and its `FitTarget`, built the
one way production builds them (`central.wall_plan.fit.fit_target`)."""
from __future__ import annotations

import math
from collections.abc import Mapping

from central.installation_models import FrameOutput
from central.wall_plan.fit import FitTarget, fit_target

Size = tuple[int, int]


def size_mm_of(width: int, height: int, inches: float | None) -> tuple[float, float] | None:
    """The physical (width, height) in mm of a picture of `width` x `height` square pixels whose
    diagonal is `inches`; None for None (its Display states no size)."""
    if inches is None:
        return None
    across = inches * 25.4 / math.hypot(width, height)
    return width * across, height * across


def frame_output_of(frame_id: str, size: Size | None, *, video: bool = True,
                    inches: float | None = None) -> FrameOutput:
    """A Frame filling an upright screen of `size` pixels (None: no screen size known, so no
    Frame output), `inches` across (None: the Display states no size)."""
    return FrameOutput(frame_id=frame_id, turn=0, video=video, display_px=size, output_px=size,
                       size_mm=None if size is None else size_mm_of(*size, inches))


def frame_outputs_of(sizes: Mapping[str, Size | None], *, video: bool = True,
                     inches: float | None = None) -> dict[str, FrameOutput]:
    """`frame_output_of` for each Frame of `sizes`, keyed by frame id (the Planner's
    `frame_outputs`)."""
    return {frame_id: frame_output_of(frame_id, size, video=video, inches=inches)
            for frame_id, size in sizes.items()}


def target_of(width: int, height: int, *, video: bool = True,
              inches: float | None = None) -> FitTarget:
    """The FitTarget of a Frame filling a `width` x `height` screen (`frame_output_of`)."""
    target = fit_target(frame_output_of("frame", (width, height), video=video, inches=inches))
    assert target is not None
    return target
