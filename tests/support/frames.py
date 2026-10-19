"""A Frame for tests (frame box run fb-12): the one place a test builds a `FrameCreate` or the body
it posts to `/v1/operator/frames`.

A Frame's shape and pixel size come from its Position box on the Display's screen as it hangs, never
from what it was created with; a test that needs a portrait or square Frame gets it from its box (or
a turn) after binding. Until the profile goes (fb-14) the seed fills a fixed 1920 x 1080, 24 in
profile and a 400 x 225 mm wall size, which nothing reads for a shape.
"""
from __future__ import annotations

from typing import NamedTuple

from central.registry import FrameCreate
from contracts.models import FrameProfile

# What the seed fills until fb-14 drops them: read by nothing for a Frame's shape.
_PROFILE_PX = (1920, 1080)
_PROFILE_INCHES = 24
_WALL_SIZE_MM = (400, 225)


class Screen(NamedTuple):
    """The screen a test's Frame fills: the Display's pixels as it hangs and its size across in
    inches (None: the Display states no size). Where a Frame's shape comes from, never the seed."""
    width_px: int
    height_px: int
    inches: float | None = None


def frame_create(frame_id: str, *, name: str | None = None, surface_id: str = "wall",
                 x_mm: float = 0, y_mm: float = 0, video: bool = True) -> FrameCreate:
    """The one place a test builds a FrameCreate: Frame `frame_id` hung at (`x_mm`, `y_mm`) on
    `surface_id`, showing videos when `video`."""
    width_px, height_px = _PROFILE_PX
    width_mm, height_mm = _WALL_SIZE_MM
    return FrameCreate(id=frame_id, name=name, surface_id=surface_id, x_mm=x_mm, y_mm=y_mm,
                       width_mm=width_mm, height_mm=height_mm,
                       profile=FrameProfile(width_px=width_px, height_px=height_px,
                                            diagonal_inches=_PROFILE_INCHES, video=video))


def frame_body(frame_id: str, *, name: str | None = None, surface_id: str = "wall",
               x_mm: float = 0, y_mm: float = 0, video: bool = True) -> dict[str, object]:
    """`frame_create(...)` as the JSON body a test posts to `/v1/operator/frames`."""
    return frame_create(frame_id, name=name, surface_id=surface_id, x_mm=x_mm, y_mm=y_mm,
                        video=video).model_dump(mode="json")
