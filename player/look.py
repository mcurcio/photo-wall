"""A composition's look at the time of a frame, and when it next changes over time: the one
evaluation of every time-varying picture property. Pure: no renderer, GL or executor state.

The executor decides what a Frame shows (its planning tick); the renderer decides how it looks,
evaluating each layer at the time of the frame it draws (its display's frame clock). A layer's
look is a function of plan time alone (contracts/models.py `fade_share` over the layer's fades, as
the seam rule below leaves them); `OutputComposition.timebase` maps the renderer's monotonic clock
onto plan time. A change that follows from time is answered by `next_change` (and every set of seen
layers a composition will take, by `moments`, which the controller budget is judged at); a change
that arrives as an event (a new composition, a video's new sample, an edit from Display Host) is the
renderer's to mark.

Rule this module owns (one home):
- **The seam rule** (`_fades`): a Frame never fades between two pictures of one drawn identity
  (player/planes.py `drawn_identity`; owner, 2026-11-15: "The player shouldn't be running any
  translations if the displayed content isn't going to change"). A layer does not fade in when the
  Frame shows its picture alone at full now (`shown`: the renderer's last commit, one plane at
  full); it does not fade out when what the Frame draws next is its own picture: the next layer to
  start during it on its Frame (of its drawn layers and `OutputComposition.following`), or, with
  none and no other layer playing through its fade-out, itself, kept ("keep_this_photo",
  contracts/on_screen.py). Only an opaque picture is merged so (player/visibility.py `opaque`): a
  PNG may carry transparency, so it shows what lies beneath and always fades. Every other fade
  is drawn as planned. Whatever path brings the next layer (a slot, a planned layer, a new plan, a
  rejoin, a fallback's kept photo), this is the one place deciding whether the screen changes.
"""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import replace

from contracts.models import Layer, fade_share
from player.planes import drawn_identity
from player.rendering import OutputComposition
from player.visibility import opaque, visible_now

Spans = tuple[tuple[float, float], tuple[float, float]]
"""A layer's fade-in and fade-out as half-open spans of plan time (contracts/models.py `Layer.fades`)."""


def _fades(composition: OutputComposition, shown: Hashable | None) -> dict[str, Spans]:
    """Each drawn layer's fades as the seam rule leaves them (module rule), by assignment id."""
    frame = (*(local.layer for local in composition.layers), *composition.following)
    result = {}
    for local in composition.layers:
        layer = local.layer
        fade_in = 0.0 if opaque(layer, layer.opacity) and drawn_identity(layer) == shown else layer.fade_in
        fade_out = 0.0 if _draws_itself_next(layer, frame) else layer.fade_out
        result[layer.assignment_id] = ((layer.start, layer.start + fade_in), (layer.end - fade_out, layer.end))
    return result


def _draws_itself_next(layer: Layer, frame: tuple[Layer, ...]) -> bool:
    """Whether what `layer`'s Frame draws next is `layer`'s own picture (module rule): the next of
    `frame` to start during it, opaque, of its identity; or, with none and nothing else in `frame`
    playing through its fade-out, `layer` kept."""
    others = [other for other in frame if other.assignment_id != layer.assignment_id]
    following = [other for other in others if layer.start < other.start <= layer.end]
    if following:
        nearest = min(following, key=lambda other: other.start)
        return opaque(nearest, nearest.opacity) and drawn_identity(nearest) == drawn_identity(layer)
    fading = layer.end - layer.fade_out
    return layer.after_end == "keep_this_photo" and not any(
        other.start < layer.end and other.end > fading for other in others)


def _alpha(layer: Layer, spans: Spans, now: float) -> float:
    """`layer`'s alpha at plan time `now` through `spans` (contracts/models.py `fade_share`)."""
    (start, faded_in), (fading, end) = spans
    return layer.opacity * fade_share(now, start=start, end=end, fade_in=faded_in - start, fade_out=end - fading)


def look(composition: OutputComposition, at: float, shown: Hashable | None = None) -> OutputComposition:
    """`composition` as it looks at `at` (seconds on this machine's monotonic clock): each layer's
    alpha at the plan time `at` maps to, through its fades as the seam rule leaves them while the
    Frame shows `shown` (module rule). A still composition (no timebase) is as given."""
    if composition.timebase is None:
        return composition
    now = at + composition.timebase
    fades = _fades(composition, shown)
    return replace(composition, layers=tuple(
        replace(local, alpha=_alpha(local.layer, fades[local.layer.assignment_id], now))
        for local in composition.layers))


def next_change(composition: OutputComposition, at: float,
                expires: float | None = None, shown: Hashable | None = None) -> float | None:
    """When what `composition` shows, drawn at `at`, next changes over time (seconds on the
    monotonic clock): `at` while a layer seen at `at` (player/visibility.py) fades; else the
    start of the next fade, or `expires` (when a drawn calibration trial runs out) if sooner;
    None when nothing changes after `at`. A covered layer's fade is unseen: what uncovers it is a
    change of the layer above. A playing video's new picture is its decoder's event, not time's.
    The fades are as the seam rule leaves them while the Frame shows `shown`."""
    times = [] if expires is None else [expires]
    if composition.timebase is not None:
        now = at + composition.timebase
        fades = _fades(composition, shown)
        seen = {local.layer.assignment_id for local in visible_now(look(composition, at, shown).layers)}
        for local in composition.layers:
            for start, end in fades[local.layer.assignment_id]:
                if start < now < end and local.layer.assignment_id in seen:
                    return at
                if now <= start < end:
                    times.append(start - composition.timebase)
    return min(times, default=None)


def fade_end(composition: OutputComposition, at: float, shown: Hashable | None = None) -> float | None:
    """When the fades seen at `at` are over: the latest end, in plan time, of a seen layer's fade
    under way at `at` (the layers player/visibility.py sees at `at`, as `next_change` reads them,
    its fades as the seam rule leaves them while the Frame shows `shown`); None when no seen layer
    fades at `at`, or for a still composition."""
    if composition.timebase is None:
        return None
    now = at + composition.timebase
    fades = _fades(composition, shown)
    seen = {local.layer.assignment_id for local in visible_now(look(composition, at, shown).layers)}
    ends = [end for local in composition.layers if local.layer.assignment_id in seen
            for start, end in fades[local.layer.assignment_id] if start < now < end]
    return max(ends, default=None)


def moments(composition: OutputComposition, at: float, shown: Hashable | None = None) -> tuple[float, ...]:
    """The frame times (monotonic) at which `composition`, drawn from `at` on, takes every set of seen
    layers it will take (render redesign S3: the controller budget is judged at each, player/
    plane_renderer.py `capacity`): `at`, then the middle of every fade of a layer of `composition`
    under way at `at` or starting after it, when that middle is after `at` (a fade under way past its
    middle holds `at` already), in time order without repeats, the fades as the seam rule leaves them
    while the Frame shows `shown`. (at,) for a still composition (no timebase). What is seen changes
    only at a fade's ends, so one moment inside each fade and `at` cover them all. Pure."""
    if composition.timebase is None:
        return (at,)
    now = at + composition.timebase
    middles = {(start + end) / 2 for spans in _fades(composition, shown).values() for start, end in spans
               if start < end and math.isfinite(start + end)}
    # A fade whose middle is past is under way at `at` (or over): `at` lies inside it already.
    return (at, *(middle - composition.timebase for middle in sorted(middles) if middle > now))
