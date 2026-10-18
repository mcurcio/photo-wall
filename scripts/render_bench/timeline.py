"""The tracer's plan, pure (bead B6): the steps, the plan's layers on the one Frame and the plane
strengths each moment must show, by the Player's own rules (player/planes.py), never a copy."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from contracts.display_turn import DisplayTurn
from contracts.frame_fit import FrameRectangle
from contracts.models import Layer, OutputBinding

StepName = Literal["hold", "dissolve", "dip", "photo_to_video", "turn", "soak", "overload"]


@dataclass(frozen=True)
class Media:
    """One file the bench shows: a photo (JPEG or PNG) or a 1080p30 HEVC video (MP4), its pixel
    size and, for a video, its duration in seconds."""
    path: Path
    kind: Literal["photo", "video"]
    size: tuple[int, int]
    duration: float | None


@dataclass(frozen=True)
class Step:
    """One part of the tracer between `start` and `end` (plan seconds), with the screen's `turn`."""
    name: StepName
    start: float
    end: float
    turn: DisplayTurn


@dataclass(frozen=True)
class Tracer:
    """The whole tracer: its steps in order, the one Frame's binding (a 16:9 profile, no lights)
    and rectangle (centred, full width), and the plan's layers (contracts.models.Layer, each media
    layer's Variant named by its file's sha256 and size, its crop centred), by stack order."""
    steps: tuple[Step, ...]
    binding: OutputBinding
    rectangle: FrameRectangle
    layers: tuple[Layer, ...]


def tracer(output_id: str, photos: Sequence[Media], video: Media, *, origin: float, fade: float = 3.0,
           hold: float = 10.0, soak: float = 600.0) -> Tracer:
    """The tracer from plan time `origin`: photo 1 held; photo 2 dissolving in over it for `fade`
    seconds (a cross-fade: the outgoing stays at full); a dip (photo 2 fading out and photo 3 in,
    half of `fade` each, black between); photo 3 dissolving into `video`; the video held; the
    screen turned 90 with photo 4 dissolving in over the video; `soak` seconds of dissolves between
    the photos; then the overload step (five full-width pictures at once, the budget's cut). Each
    fade is followed by `hold` seconds. ValueError("tracer_media") unless there are at least four
    photos and one video. Pure."""
    raise NotImplementedError("render-planes S1 bead B6")


def strengths_at(tracer: Tracer, at: float, screen_size: tuple[int, int]) -> dict[str, float]:
    """The plane strength each seen layer must have at plan time `at` (assignment id -> strength),
    through player/look.py `look` and player/planes.py `compose` on a screen of `screen_size` as it
    hangs. Pure."""
    raise NotImplementedError("render-planes S1 bead B6")
