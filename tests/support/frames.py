"""A Frame for tests (frame box run fb-12): the one place a test builds a `FrameCreate` or the body
it posts to `/v1/operator/frames`.

A Frame's shape and pixel size come from its Position box on the Display's screen as it hangs, never
from what it was created with; a test that needs a portrait or square Frame gets it from its box (or
a turn) after binding.
"""
from __future__ import annotations

from typing import NamedTuple

from central.registry import FrameCreate


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
    return FrameCreate(id=frame_id, name=name, surface_id=surface_id, x_mm=x_mm, y_mm=y_mm, video=video)


def frame_body(frame_id: str, *, name: str | None = None, surface_id: str = "wall",
               x_mm: float = 0, y_mm: float = 0, video: bool = True) -> dict[str, object]:
    """`frame_create(...)` as the JSON body a test posts to `/v1/operator/frames`."""
    return frame_create(frame_id, name=name, surface_id=surface_id, x_mm=x_mm, y_mm=y_mm,
                        video=video).model_dump(mode="json")
