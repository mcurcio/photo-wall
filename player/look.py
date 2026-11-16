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
  Frame shows its picture alone at full (`OutputComposition.shown`: `alone` of what its Output last
  acknowledged drawing); it does not fade out when what the Frame draws next is its own picture: the
  topmost layer playing on its Frame as it ends (of its drawn layers and
  `OutputComposition.following`; a layer gone by then, or covered then, does not count), or, with
  none and no other layer playing through its fade-out, itself, kept (contracts/
  on_screen.py "keep_this_photo"; the executor keeps it from each draw of it). Only an opaque picture is
  merged so (player/visibility.py `opaque`): a PNG may carry transparency, so it shows what lies
  beneath and always fades. Every other fade is drawn as planned. Whatever path brings the next
  layer (a slot, a planned layer, a new plan, a rejoin, a fallback's kept photo), this is the one
  place deciding whether the screen changes, and its inputs are the composition's alone: the
  executor (`look_at`: what it reads as seen) and the renderer (`look`: what it draws) look one
  composition alike. A composition the executor holds past its layers' ends (`held`: the rejoin
  hold, decision 0032), its hand-over to the new plan's layer (`handed_over`: the held layers
  beneath the new ones until those hide them, one more composition) and the photo a Frame keeps
  (player/executor.py `_fallback`) are looked through it too, never given an alpha of their own (owner, 2026-11-15: "There *should* only be
  one place where the photo can change").
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from dataclasses import replace

from contracts.models import Layer, fade_share, stack_order
from player.planes import drawn_identity
from player.rendering import LocalLayer, OutputComposition
from player.visibility import opaque, visible_now

Spans = tuple[tuple[float, float], tuple[float, float]]
"""A layer's fade-in and fade-out as half-open spans of plan time (contracts/models.py `Layer.fades`)."""


def alone(layers: Sequence[LocalLayer]) -> Hashable | None:
    """The picture one Frame's `layers` (at their drawn alphas, bottom to top) show alone at full:
    the drawn identity of their one seen layer (player/visibility.py `visible_now`) when it is
    opaque at its alpha; None otherwise. What the seam rule's `OutputComposition.shown` holds."""
    seen = visible_now(layers)
    if len(seen) != 1 or not opaque(seen[0].layer, seen[0].alpha):
        return None
    return drawn_identity(seen[0].layer)


def _fades(composition: OutputComposition) -> dict[str, Spans]:
    """Each drawn layer's fades as the seam rule leaves them (module rule), by assignment id."""
    frame = (*(local.layer for local in composition.layers), *composition.following)
    result = {}
    for local in composition.layers:
        layer = local.layer
        # `shown` is only ever an opaque picture's identity (`alone`), so a layer equal to it is opaque.
        fade_in = 0.0 if drawn_identity(layer) == composition.shown else layer.fade_in
        fade_out = 0.0 if _draws_itself_next(layer, frame) else layer.fade_out
        result[layer.assignment_id] = ((layer.start, layer.start + fade_in), (layer.end - fade_out, layer.end))
    return result


def _draws_itself_next(layer: Layer, frame: tuple[Layer, ...]) -> bool:
    """Whether what `layer`'s Frame draws next is `layer`'s own picture (module rule): the topmost
    (contracts/models.py `stack_order`) of the rest of `frame` playing as `layer` ends, opaque, of
    its identity; or, with none and nothing else in `frame` playing through its fade-out, `layer`
    kept (an opaque still, contracts/models.py). A layer of its picture that is gone by its end, or
    covered then, is not what follows it."""
    others = [other for other in frame if other.assignment_id != layer.assignment_id]
    following = [other for other in others if other.start <= layer.end < other.end]
    if following:
        return _same(max(following, key=stack_order), layer)
    fading = layer.end - layer.fade_out
    return layer.after_end == "keep_this_photo" and not any(
        other.start < layer.end and other.end > fading for other in others)


def _same(next_layer: Layer, layer: Layer) -> bool:
    """Whether `next_layer`, drawn at its opacity, shows exactly `layer`'s picture and hides what
    lies beneath it (module rule)."""
    return opaque(next_layer, next_layer.opacity) and drawn_identity(next_layer) == drawn_identity(layer)


def _alpha(layer: Layer, spans: Spans, now: float) -> float:
    """`layer`'s alpha at plan time `now` through `spans` (contracts/models.py `fade_share`)."""
    (start, faded_in), (fading, end) = spans
    return layer.opacity * fade_share(now, start=start, end=end, fade_in=faded_in - start, fade_out=end - fading)


def look_at(composition: OutputComposition, now: float) -> OutputComposition:
    """`composition` as it looks at plan time `now`: each layer's alpha through its fades as the
    seam rule leaves them (module rule). The executor's look of what it makes (player/executor.py),
    the same as the renderer's at the frame time `now` maps to (`look`)."""
    fades = _fades(composition)
    return replace(composition, layers=tuple(
        replace(local, alpha=_alpha(local.layer, fades[local.layer.assignment_id], now))
        for local in composition.layers))


def held(composition: OutputComposition, now: float) -> OutputComposition:
    """`composition` kept on its Frame past its layers' ends (decision 0032, the rejoin hold), for
    `look_at` and `look` to draw like any other: a layer whose fade-out as the seam rule leaves it
    (module rule) is empty or has not begun at plan time `now` draws itself next, so its fade-out
    is left out and it keeps its picture past its end; a fade-in under way runs on and a fade-out
    under way runs to its end, as drawn. So holding a composition never changes its picture: a
    dissolve caught by the hold completes as planned. Pure."""
    fades = _fades(composition)

    def kept(local: LocalLayer) -> LocalLayer:
        fading, end = fades[local.layer.assignment_id][1]
        if fading < end and now >= fading:
            return local
        return replace(local, layer=local.layer.model_copy(update={"fade_out": 0.0}))

    return replace(composition, layers=tuple(kept(local) for local in composition.layers))


def handed_over(incoming: OutputComposition, kept: Sequence[LocalLayer], now: float) -> OutputComposition:
    """`incoming` (a new plan's composition on a Frame) with `kept` beneath it, the layers of the
    composition it supersedes as `held` keeps them, until `incoming` hides them (decision 0032,
    rejoining shows no black): so the new plan's layer fades in over the picture the Frame holds,
    as any dissolve does, never over black; looked at plan time `now` (`look_at`). `kept` stays
    until the first of `incoming`'s opaque layers is faded in, as the seam rule leaves its fade (a
    layer of the picture shown alone at full does not fade in, so the hand-over is over at its
    start: the picture is kept), and ends then, so the executor leaves it out from that time on.
    With no opaque layer `incoming` never hides `kept`, so `kept` fades out over its latest fade-in
    instead, never cut. `incoming` alone once the hand-over is over. Pure."""
    fades = _fades(incoming)
    faded_in = [(fades[local.layer.assignment_id][0], opaque(local.layer, local.layer.opacity))
                for local in incoming.layers]
    covering = [end for (_, end), hides in faded_in if hides]
    if covering:
        begin = until = min(covering)
    else:
        begin, until = max((span for span, _ in faded_in), key=lambda span: span[1], default=(now, now))
    if now >= until:
        return incoming

    def beneath(local: LocalLayer) -> LocalLayer:
        layer = local.layer
        if layer.fade_out and now >= layer.end - layer.fade_out:
            return local                    # a fade-out under way runs on as drawn (`held`)
        return replace(local, layer=layer.model_copy(update={"end": until, "fade_out": until - begin}))

    return look_at(replace(incoming, layers=(*map(beneath, kept), *incoming.layers)), now)


def look(composition: OutputComposition, at: float) -> OutputComposition:
    """`composition` as it looks at `at` (seconds on this machine's monotonic clock): `look_at` the
    plan time `at` maps to. A still composition (no timebase) is as given."""
    if composition.timebase is None:
        return composition
    return look_at(composition, at + composition.timebase)


def next_change(composition: OutputComposition, at: float, expires: float | None = None) -> float | None:
    """When what `composition` shows, drawn at `at`, next changes over time (seconds on the
    monotonic clock): `at` while a layer seen at `at` (player/visibility.py) fades; else the
    start of the next fade, or `expires` (when a drawn calibration trial runs out) if sooner;
    None when nothing changes after `at`. A covered layer's fade is unseen: what uncovers it is a
    change of the layer above. A playing video's new picture is its decoder's event, not time's.
    The fades are as the seam rule leaves them."""
    times = [] if expires is None else [expires]
    if composition.timebase is not None:
        now = at + composition.timebase
        fades = _fades(composition)
        seen = {local.layer.assignment_id for local in visible_now(look(composition, at).layers)}
        for local in composition.layers:
            for start, end in fades[local.layer.assignment_id]:
                if start < now < end and local.layer.assignment_id in seen:
                    return at
                if now <= start < end:
                    times.append(start - composition.timebase)
    return min(times, default=None)


def fade_end(composition: OutputComposition, at: float) -> float | None:
    """When the fades seen at `at` are over: the latest end, in plan time, of a seen layer's fade
    under way at `at` (the layers player/visibility.py sees at `at`, as `next_change` reads them,
    its fades as the seam rule leaves them); None when no seen layer fades at `at`, or for a still
    composition."""
    if composition.timebase is None:
        return None
    now = at + composition.timebase
    fades = _fades(composition)
    seen = {local.layer.assignment_id for local in visible_now(look(composition, at).layers)}
    ends = [end for local in composition.layers if local.layer.assignment_id in seen
            for start, end in fades[local.layer.assignment_id] if start < now < end]
    return max(ends, default=None)


def moments(composition: OutputComposition, at: float) -> tuple[float, ...]:
    """The frame times (monotonic) at which `composition`, drawn from `at` on, takes every set of seen
    layers it will take (render redesign S3: the controller budget is judged at each, player/
    plane_renderer.py `capacity`): `at`, then the middle of every fade of a layer of `composition`
    under way at `at` or starting after it, when that middle is after `at` (a fade under way past its
    middle holds `at` already), in time order without repeats, the fades as the seam rule leaves
    them. (at,) for a still composition (no timebase). What is seen changes only at a fade's ends,
    so one moment inside each fade and `at` cover them all. Pure."""
    if composition.timebase is None:
        return (at,)
    now = at + composition.timebase
    middles = {(start + end) / 2 for spans in _fades(composition).values() for start, end in spans
               if start < end and math.isfinite(start + end)}
    # A fade whose middle is past is under way at `at` (or over): `at` lies inside it already.
    return (at, *(middle - composition.timebase for middle in sorted(middles) if middle > now))
