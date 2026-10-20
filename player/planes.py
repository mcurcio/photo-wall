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
- **A shown picture keeps its plane** (`compose`, `_stacked`): Weston gives a screen's planes the
  display controller's overlays from the top down (libweston/backend-drm/state-propose.c
  `drm_output_find_plane_for_view`), so a plane added on top would move every picture beneath it
  to another overlay, and the test Pi shows nothing from an overlay a commit switches on for its
  first refresh (owner, 2026-10-19: "it shows a black frame at the start of the transition"). So
  when a layer fades in over an opaque one and would itself be opaque at full (not a PNG, its
  opacity 1), the two swap: the incoming at full beneath, the one already shown on top at
  1 - the incoming's strength. The controller mixes stored values (a * top + (1 - a) * bottom),
  so the picture is the same at every pixel; the new picture enters beneath, covered, and the
  shown one keeps its overlay until it leaves. A PNG (it may carry transparency) or a layer of
  opacity below 1 cannot be swapped and fades in on top, as does a layer over one not opaque.
- **Removed, never transparent**: a plane under MIN_STRENGTH is left out of the composition (bench
  T3: an opaque picture at alpha 0 is not black; the plane is removed instead).
- **The finish size** (`finish_size`, `Plane.size`): a picture (a still photo or a video's frame) is
  finished (fit, crop, look, LUT) at the smaller of its source's cropped size and its Frame's
  placement, scaled down evenly so the Frame's aspect is kept, and the display controller enlarges
  it to the placement (owner, 2026-10-18: "the look-and-feel should be applied at the minimum of the
  asset size and the frame size"). So the controller only ever enlarges or shows a picture 1:1,
  never shrinks it (bench T2: shrinking large sources glitches), and no picture is shown from a
  buffer larger than it appears. A black layer's picture is the placement's size.

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
from contracts.models import Layer
from contracts.photo_crop import CENTRED, crop_window
from player.geometry import encode_srgb
from player.rendering import LocalLayer
from player.visibility import opaque, visible_now

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


def finish_size(layer: Layer, at: Placement) -> tuple[int, int]:
    """The (width, height), as it hangs, at which `layer`'s picture is finished for a Frame placed
    at `at` (module rule): for a media layer, the placement scaled by
    s = min(1, cropped width / at.width, cropped height / at.height), each side rounded half up
    (floor(v + 0.5)) and at least 1 (so never larger than the placement); the cropped size is the crop window
    (contracts/photo_crop.py `crop_window` of the layer's crop at the placement's aspect) on the
    variant's width x height. A black layer: the placement's size. Pure; the one home of the size a
    picture is finished at, which the fit pass, a video's targets and the budget all ask here."""
    if layer.presentation != "media" or layer.variant is None:
        return at.width, at.height
    width, height = layer.variant.width, layer.variant.height
    left, top, right, bottom = crop_window(layer.crop or CENTRED, width, height, at.width / at.height)
    scale = min(1.0, (right - left) * width / at.width, (bottom - top) * height / at.height)
    return max(1, math.floor(at.width * scale + 0.5)), max(1, math.floor(at.height * scale + 0.5))


@dataclass(frozen=True)
class Plane:
    """One picture the controller places: the layer it shows (a media layer's finished picture, or a
    black layer's black picture), at its Frame's placement `at` (the plane's destination), at
    `strength` (its plane alpha), MIN_STRENGTH <= strength <= 1. Its picture is `size` as it hangs
    (the plane's source), which the controller enlarges to `at` when smaller (`enlarged`)."""
    frame_id: str
    layer: LocalLayer
    at: Placement
    strength: float

    @property
    def size(self) -> tuple[int, int]:
        """The plane's picture as it hangs: `finish_size(layer, at)` (module rule)."""
        return finish_size(self.layer.layer, self.at)

    @property
    def enlarged(self) -> bool:
        """Whether the controller enlarges the plane's picture to its placement."""
        return self.size != (self.at.width, self.at.height)


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
    rule), a layer fading in over an opaque one beneath it at full and that one above at the rest
    (module rule, A shown picture keeps its plane), a plane under MIN_STRENGTH left out. Pure.

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
        planes.extend(Plane(frame.frame_id, local, at, strength)
                      for local, strength in _stacked(visible_now(frame.layers)) if strength >= MIN_STRENGTH)
    return Composition(screen, tuple(planes))


def _stacked(seen: Sequence[LocalLayer]) -> list[tuple[LocalLayer, float]]:
    """One Frame's `seen` layers (bottom to top) as planes bottom to top, each with its strength:
    `plane_strength` over what lies beneath it in stack order (module rule, Which strength
    applies), the lowest two swapped when a shown picture keeps its plane (module rule)."""
    stacked, beneath = [], None
    for local in seen:
        stacked.append((local, plane_strength(local.alpha, beneath is not None
                                              and beneath.layer.presentation == "media")))
        beneath = local
    if len(seen) >= 2 and opaque(seen[0].layer, seen[0].alpha) and opaque(seen[1].layer, seen[1].layer.opacity):
        (shown, _), (entering, strength) = stacked[:2]
        stacked[:2] = [(entering, 1.0), (shown, 1.0 - strength)]
    return stacked


__all__ = ["MIN_STRENGTH", "Composition", "FrameLayers", "Plane", "Screen", "compose", "finish_size", "plane_strength"]
