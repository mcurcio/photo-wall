"""A composition's look at the time of a frame, and when it next changes over time: the one
evaluation of every time-varying picture property. Pure: no renderer, GL or executor state.

The executor decides what a Frame shows (its planning tick); the renderer decides how it looks,
evaluating each layer at the time of the frame it draws (its display's frame clock). A layer's
look is a function of plan time alone (contracts/models.py `Layer.alpha`, changing only during
`Layer.fades`); `OutputComposition.timebase` maps the renderer's monotonic clock onto plan time.
A change that follows from time is answered by `next_change` (and every set of seen layers a
composition will take, by `moments`, which the controller budget is judged at); a change that arrives as an event
(a new composition, a video's new sample, an edit from Display Host) is the renderer's to mark.
"""

from __future__ import annotations

import math
from dataclasses import replace

from player.rendering import OutputComposition
from player.visibility import visible_now


def look(composition: OutputComposition, at: float) -> OutputComposition:
    """`composition` as it looks at `at` (seconds on this machine's monotonic clock): each layer's
    alpha at the plan time `at` maps to. A still composition (no timebase) is as given."""
    if composition.timebase is None:
        return composition
    now = at + composition.timebase
    return replace(composition, layers=tuple(
        replace(local, alpha=local.layer.alpha(now)) for local in composition.layers))


def next_change(composition: OutputComposition, at: float,
                expires: float | None = None) -> float | None:
    """When what `composition` shows, drawn at `at`, next changes over time (seconds on the
    monotonic clock): `at` while a layer seen at `at` (player/visibility.py) fades; else the
    start of the next fade, or `expires` (when a drawn calibration trial runs out) if sooner;
    None when nothing changes after `at`. A covered layer's fade is unseen: what uncovers it is a
    change of the layer above. A playing video's new picture is its decoder's event, not time's."""
    times = [] if expires is None else [expires]
    if composition.timebase is not None:
        now = at + composition.timebase
        seen = {local.layer.assignment_id for local in visible_now(look(composition, at).layers)}
        for local in composition.layers:
            for start, end in local.layer.fades():
                if start < now < end and local.layer.assignment_id in seen:
                    return at
                if now <= start < end:
                    times.append(start - composition.timebase)
    return min(times, default=None)


def fade_end(composition: OutputComposition, at: float) -> float | None:
    """When the fades seen at `at` are over: the latest end, in plan time, of a seen layer's fade
    under way at `at` (the layers player/visibility.py sees at `at`, as `next_change` reads them);
    None when no seen layer fades at `at`, or for a still composition."""
    if composition.timebase is None:
        return None
    now = at + composition.timebase
    seen = {local.layer.assignment_id for local in visible_now(look(composition, at).layers)}
    ends = [end for local in composition.layers if local.layer.assignment_id in seen
            for start, end in local.layer.fades() if start < now < end]
    return max(ends, default=None)


def moments(composition: OutputComposition, at: float) -> tuple[float, ...]:
    """The frame times (monotonic) at which `composition`, drawn from `at` on, takes every set of seen
    layers it will take (render redesign S3: the controller budget is judged at each, player/
    plane_renderer.py `capacity`): `at`, then the middle of every fade of a layer of `composition`
    under way at `at` or starting after it, when that middle is after `at` (a fade under way past its
    middle holds `at` already), in time order without repeats. (at,) for a still
    composition (no timebase). What is seen changes only at a fade's ends, so one moment inside each
    fade and `at` cover them all. Pure."""
    if composition.timebase is None:
        return (at,)
    now = at + composition.timebase
    middles = {(start + end) / 2 for local in composition.layers for start, end in local.layer.fades()
               if start < end and math.isfinite(start + end)}
    # A fade whose middle is past is under way at `at` (or over): `at` lies inside it already.
    return (at, *(middle - composition.timebase for middle in sorted(middles) if middle > now))
