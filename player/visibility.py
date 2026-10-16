"""Which of a Frame's layers can be seen, now and over the next few seconds: the one rule the
executor spends decoder slots by and the native renderer draws by ([execution contract › The
bound](../docs/execution-contract.md)). Pure: no renderer, clock or plan state.

Every layer fills its whole Frame (a Frame fit applies to the Frame, never to one layer), so a layer
hides everything beneath it exactly while it is opaque: at full strength and not a PNG, which may
carry transparency. A black layer is opaque. Bottom to top is `contracts.models.stack_order`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from contracts.models import Layer, stack_order
from player.rendering import LocalLayer

Span = tuple[float, float]
"""A half-open interval of wall-clock seconds."""


def opaque(layer: Layer, alpha: float) -> bool:
    """Whether `layer` drawn at `alpha` hides everything beneath it."""
    return alpha >= 1 and (layer.variant is None or layer.variant.media_type != "image/png")


def opaque_span(layer: Layer) -> Span | None:
    """When `layer` is opaque: from the end of its fade-in to the start of its fade-out (its alpha,
    `player/executor.py` `_alpha`, is 1 exactly then); None when it never is."""
    if not opaque(layer, layer.opacity):
        return None
    start, end = layer.start + layer.fade_in, layer.end - layer.fade_out
    return (start, end) if start < end else None


def visible_now(layers: Sequence[LocalLayer]) -> tuple[LocalLayer, ...]:
    """Of one Frame's layers at their drawn alphas, bottom to top, those that can be seen: every
    layer with some alpha down to the topmost opaque one."""
    visible = []
    for local in reversed(layers):
        if local.alpha > 0:
            visible.append(local)
        if opaque(local.layer, local.alpha):
            break
    return tuple(reversed(visible))


def hidden(span: Span, covers: Iterable[Span], since: float, until: float) -> bool:
    """Whether something present over `span` is covered by `covers` at every instant it is present
    from `since` through `until`, both included."""
    low = max(since, span[0])
    if low >= span[1] or low > until:
        return True
    reach = low
    for start, end in sorted(covers):
        if start > reach:
            break
        reach = max(reach, end)
    return reach >= span[1] if until >= span[1] else reach > until


def _covers(covering: Iterable[Layer], above: tuple | None, horizon: float) -> list[Span]:
    spans = []
    for layer in covering:
        span = opaque_span(layer)
        if span and (above is None or stack_order(layer) > above):
            spans.append((span[0], min(span[1], horizon)))
    return spans


def shown_within(layers: Iterable[Layer], covering: Sequence[Layer], since: float, until: float,
                 *, horizon: float = math.inf) -> set[str]:
    """The assignments of one Frame's `layers` seen at some instant from `since` through `until`:
    those not covered throughout by an opaque `covering` layer above them (opaque until `horizon`
    at most, the plan's end)."""
    return {layer.assignment_id for layer in layers
            if not hidden((layer.start, layer.end),
                          _covers(covering, stack_order(layer), horizon), since, until)}


def beneath_shown(covering: Sequence[Layer], since: float, until: float,
                  *, horizon: float = math.inf) -> bool:
    """Whether what a Frame keeps for when nothing plays, beneath all its layers, is seen at some
    instant from `since` through `until`."""
    return not hidden((-math.inf, math.inf), _covers(covering, None, horizon), since, until)


def shed(frames: Mapping[str, Sequence[Layer]]) -> Layer | None:
    """The layer given up when decoder slots run short: the bottom-most of the Frame drawing the
    most layers (ties: the lower bottom layer), so a screen keeps its top photo; never a Frame's
    only layer (None when each Frame has one)."""
    choices = [sorted(layers, key=stack_order) for layers in frames.values() if len(layers) > 1]
    if not choices:
        return None
    return min(choices, key=lambda ordered: (-len(ordered), stack_order(ordered[0])))[0]
