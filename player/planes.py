"""A screen's composition: which pictures the display controller places, where, stacked how and at
what strength, at one frame time (render redesign, run ledger .claude/runs/render-planes/
s1-page.md). Pure: no GPU, Wayland, GStreamer or clock.

The Player decides every pixel and Display Host's shell only applies it (design rule 1). A
composition is a list of planes, bottom to top, even while Central keeps one Frame per screen
(owner, gate answer Q4 "later", 2026-10-18), so more Frames per screen need no new shape.

Rules this module owns (one home each):
- **The plane strength** (`plane_strength`): the controller mixes stored sRGB values, not light
  (bench T3), so a layer's drawn alpha (its share of its opacity, contracts/models.py
  `Layer.alpha`, through `fade_share`) becomes its plane's alpha by what lies beneath it: over a
  picture (a dissolve, or a Run playing beneath) the alpha itself, the outgoing picture opaque as
  the plan already keeps it; over black (a dip) `encode_srgb(alpha)` (player/geometry.py), within
  6 levels of today's linear-light mix.
- **Which strength applies** (`compose`): over a picture exactly when the nearest seen layer
  beneath it on the same Frame is a media layer; otherwise over black. What can be seen is
  player/visibility.py `visible_now`'s, never a second rule.
- **Removed, never transparent**: a plane under MIN_STRENGTH is left out of the composition (bench
  T3: an opaque picture at alpha 0 is not black; the plane is removed instead).

Rules it does NOT own: the fade curve (contracts/models.py `fade_share`); a layer's alpha at a frame
time (player/look.py `look`); the controller's limits (player/controller_budget.py); pixels and
placement (contracts/frame_fit.py `place`); the turn (contracts/display_turn.py).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from contracts.display_turn import DisplayTurn
from contracts.frame_fit import Placement
from player.geometry import encode_srgb
from player.rendering import LocalLayer
from player.visibility import visible_now

MIN_STRENGTH: Final = 1 / 255
"""The weakest plane shown: the controller's 8-bit plane alpha rounds anything below to 0."""


@dataclass(frozen=True)
class Screen:
    """One Output as Weston reports it: its id, its size in pixels as it hangs (contracts/
    display_turn.py `hanging` of the current mode) and its turn (`turn_of` of the Output's
    `wl_output.transform`)."""
    output_id: str
    size: tuple[int, int]
    turn: DisplayTurn


@dataclass(frozen=True)
class FrameLayers:
    """One Frame on a screen at one frame time: its placement (contracts/frame_fit.py `place`) and
    its layers bottom to top (contracts/models.py `stack_order`) at their drawn alphas
    (player/look.py `look`)."""
    frame_id: str
    placement: Placement
    layers: tuple[LocalLayer, ...]


@dataclass(frozen=True)
class Plane:
    """One picture the controller places: the layer it shows (a media layer's fitted picture, or a
    black layer's black picture of the same size), at its Frame's placement, at `strength` (its
    plane alpha), MIN_STRENGTH <= strength <= 1."""
    frame_id: str
    layer: LocalLayer
    at: Placement
    strength: float


@dataclass(frozen=True)
class Composition:
    """One screen at one frame time: its planes bottom to top (the Frames in the order given, each
    Frame's seen layers by stack order). Built only by `compose`."""
    screen: Screen
    planes: tuple[Plane, ...]


def plane_strength(alpha: float, over_picture: bool) -> float:
    """A layer's plane alpha for its drawn `alpha` (module rule): `alpha` over a picture,
    `encode_srgb(alpha)` over black. ValueError("plane_alpha") unless `alpha` is a finite int or
    float (never a bool) in 0..1. Pure."""
    if not (isinstance(alpha, (int, float)) and not isinstance(alpha, bool)
            and math.isfinite(alpha) and 0 <= alpha <= 1):
        raise ValueError("plane_alpha")
    return float(alpha) if over_picture else encode_srgb(alpha)


def compose(screen: Screen, frames: Sequence[FrameLayers]) -> Composition:
    """`screen`'s composition: for each Frame in order, its seen layers (`visible_now`) bottom to
    top, each a Plane at the Frame's placement at `plane_strength(alpha, over_picture)` (module
    rule), a plane under MIN_STRENGTH left out. Pure.

    ValueError("compose_frames") for two FrameLayers with one frame_id; ValueError("compose_outside")
    unless every placement lies inside `screen.size` (0 <= x, x + width <= W, 0 <= y,
    y + height <= H); ValueError("compose_output") unless every layer's `output_id` is
    `screen.output_id`."""
    if len({frame.frame_id for frame in frames}) != len(frames):
        raise ValueError("compose_frames")
    width, height = screen.size
    planes = []
    for frame in frames:
        at = frame.placement
        if not (0 <= at.x and at.x + at.width <= width and 0 <= at.y and at.y + at.height <= height):
            raise ValueError("compose_outside")
        if any(local.layer.output_id != screen.output_id for local in frame.layers):
            raise ValueError("compose_output")
        beneath = None
        for local in visible_now(frame.layers):
            over_picture = beneath is not None and beneath.layer.presentation == "media"
            strength = plane_strength(local.alpha, over_picture)
            if strength >= MIN_STRENGTH:
                planes.append(Plane(frame.frame_id, local, at, strength))
            beneath = local
    return Composition(screen, tuple(planes))


__all__ = ["MIN_STRENGTH", "Composition", "FrameLayers", "Plane", "Screen", "compose", "plane_strength"]
