"""A composition's look at the time of a frame: the one evaluation of every time-varying picture
property, and whether the look still changes. Pure: no renderer, GTK or executor state.

The executor decides what a Frame shows (its planning tick); the renderer decides how it looks,
evaluating each layer at the time of the frame it draws (its display's frame clock). A layer's
look is a function of plan time alone (contracts/models.py `Layer.position`, `Layer.alpha`,
`Layer.motion`); `OutputComposition.timebase` maps the renderer's monotonic clock onto plan time.
A new time-varying property is drawn on every frame by joining `Layer.motion`, with no timer of
its own.
"""

from __future__ import annotations

from dataclasses import replace

from player.rendering import OutputComposition
from player.visibility import opaque


def look(composition: OutputComposition, at: float) -> OutputComposition:
    """`composition` as it looks at `at` (seconds on this machine's monotonic clock): each layer's
    position and alpha at the plan time `at` maps to. A still composition (no timebase) is as
    given."""
    if composition.timebase is None:
        return composition
    now = at + composition.timebase
    return replace(composition, layers=tuple(
        replace(local, position=local.layer.position(now), alpha=local.layer.alpha(now))
        for local in composition.layers))


def until_change(composition: OutputComposition, at: float) -> float | None:
    """Seconds from `at` until `composition`'s look next changes: 0 while a layer not covered at
    `at` (by an opaque one above it) is in motion, None when nothing changes after `at` (a still
    picture, or a still composition). A covered layer's motion is unseen until what covers it
    changes, itself a change here or a new composition."""
    if composition.timebase is None:
        return None
    now = at + composition.timebase
    layers = look(composition, at).layers
    top = max((index for index, local in enumerate(layers) if opaque(local.layer, local.alpha)),
              default=0)
    uncovered = {local.layer.assignment_id for local in layers[top:]}
    waits = []
    for local in composition.layers:
        for start, end in local.layer.motion():
            if start > now:
                waits.append(start - now)
            elif now < end and local.layer.assignment_id in uncovered:
                return 0.0
    return min(waits, default=None)


def changed(composition: OutputComposition, since: float, at: float) -> bool:
    """Whether `composition`'s look at `at` may differ from its look at `since`, an earlier
    frame's time: a change began between them."""
    wait = until_change(composition, since)
    return wait is not None and wait <= at - since
