"""A composition's look at the time of a frame, and when it next changes over time: the one
evaluation of every time-varying picture property. Pure: no renderer, GTK or executor state.

The executor decides what a Frame shows (its planning tick); the renderer decides how it looks,
evaluating each layer at the time of the frame it draws (its display's frame clock). A layer's
look is a function of plan time alone (contracts/models.py `Layer.alpha`, changing only during
`Layer.fades`); `OutputComposition.timebase` maps the renderer's monotonic clock onto plan time.
A change that follows from time is answered by `next_change`; a change that arrives as an event
(a new composition, a video's new sample, an edit from Display Host) is the renderer's to mark.
"""

from __future__ import annotations

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
