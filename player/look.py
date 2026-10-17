"""A composition's look at the time of a frame: the one evaluation of every time-varying picture
property, and whether what it shows still changes. Pure: no renderer, GTK or executor state.

The executor decides what a Frame shows (its planning tick); the renderer decides how it looks,
evaluating each layer at the time of the frame it draws (its display's frame clock). A layer's
look is a function of plan time alone (contracts/models.py `Layer.alpha`, changing only during
`Layer.fades`); `OutputComposition.timebase` maps the renderer's monotonic clock onto plan time.
A playing video's picture is its decoder's newest sample, which the renderer compares on each
frame while `until_change` says one may come. A new time-varying property is drawn on every frame
by joining `look` and `until_change`, with no timer of its own.
"""

from __future__ import annotations

from dataclasses import replace

from contracts.models import Layer
from player.rendering import OutputComposition
from player.visibility import opaque

TIMEBASE_TOLERANCE = 0.001
"""Seconds two timebases may differ by and still map one timeline: the executor re-anchors its
clock mapping on every planning tick, which moves it by microseconds."""


def look(composition: OutputComposition, at: float) -> OutputComposition:
    """`composition` as it looks at `at` (seconds on this machine's monotonic clock): each layer's
    alpha at the plan time `at` maps to. A still composition (no timebase) is as given."""
    if composition.timebase is None:
        return composition
    now = at + composition.timebase
    return replace(composition, layers=tuple(
        replace(local, alpha=local.layer.alpha(now)) for local in composition.layers))


def retimed(drawn: OutputComposition, composition: OutputComposition) -> bool:
    """Whether `composition` maps this machine's clock onto plan time otherwise than `drawn` (a
    re-anchored clock mapping, or one of them still): a renderer drawing `drawn`'s layers on its
    frame clock would draw them at the wrong plan time."""
    if drawn.timebase is None or composition.timebase is None:
        return drawn.timebase != composition.timebase
    return abs(drawn.timebase - composition.timebase) > TIMEBASE_TOLERANCE


def _plays(layer: Layer) -> bool:
    return layer.variant is not None and bool(layer.variant.duration)


def until_change(composition: OutputComposition, at: float, *,
                 pictures: bool = True) -> float | None:
    """Seconds from `at` until what `composition` shows next changes: 0 while a layer not covered
    at `at` (by an opaque one above it) fades or, with `pictures`, is a playing video (its
    decoder brings new pictures on its own); None when nothing changes after `at` (a still
    picture, or a still composition). A covered layer's change is unseen until what covers it
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
        layer = local.layer
        spans = [span for span in layer.fades() if span[0] < span[1]]
        if pictures and _plays(layer):
            spans.append((layer.start, layer.end))
        for start, end in spans:
            if start > now:
                waits.append(start - now)
            elif now < end and layer.assignment_id in uncovered:
                return 0.0
    return min(waits, default=None)


def changed(composition: OutputComposition, since: float, at: float) -> bool:
    """Whether `composition`'s look (`look`) at `at` may differ from its look at `since`, an
    earlier frame's time: a fade began or ran between them. A video's new picture is not a
    plan-time change; the renderer compares its decoder's samples."""
    wait = until_change(composition, since, pictures=False)
    return wait is not None and wait <= at - since
