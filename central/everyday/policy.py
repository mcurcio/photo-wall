"""The everyday actions' rules, each with one home (decision 0036, "Slice 3 design"). Pure.

`stays` is the one home of who can move: Frame status calls it for `can` and Coordination's
command calls it again under its lock, so `can` and the answer agree on one state. Central never
changes an offered layer for an everyday action: it adds a layer above it (`cover_for`).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence

from central.everyday.model import (
    ACK_LATE_KEPT_SECONDS,
    ACK_LATE_SECONDS,
    END_LEAD,
    SKIP_LEAD,
    AskState,
    Cover,
    CoverCause,
    PauseRefusal,
    SkipKind,
    StayReason,
)
from central.everyday.ports import FrameFacts, LayerOrigin, ShownPhoto
from contracts.models import Layer, stack_order


def _opaque(layer: Layer, alpha: float) -> bool:
    """Whether `layer` drawn at `alpha` hides everything beneath it: at full strength and not a
    PNG. Restates player/visibility.py `opaque` (central never imports the Player), held equal by
    tests/test_everyday_policy.py over shared cases."""
    return alpha >= 1 and (layer.variant is None or layer.variant.media_type != "image/png")


def showing_at(layers: Iterable[Layer], at: float) -> Layer | None:
    """The layer a Frame shows at `at`: of its layers drawn then, the topmost by `stack_order` that
    is opaque at its alpha then (what covers everything beneath it, the Player's covered rule),
    else the topmost with any strength; None when none is drawn. The target of every action."""
    drawn = sorted((layer for layer in layers if layer.start <= at < layer.drawn_end), key=stack_order)
    for layer in reversed(drawn):
        if _opaque(layer, layer.alpha(at)):
            return layer
    return next((layer for layer in reversed(drawn) if layer.alpha(at) > 0), None)


def the_shared_instant(now: float) -> float:
    """When the Frames of one request move together: `SKIP_LEAD` after it is asked."""
    return now + SKIP_LEAD


def photo_of(layer: Layer | None) -> str | None:
    """The photo a layer shows (its prepared copy names it), None for a black one."""
    return None if layer is None or layer.variant is None else layer.variant.asset_id


def cover_id(request_id: str, frame_id: str) -> str:
    """A cover's assignment id: one per request and Frame."""
    encoded = json.dumps([request_id, frame_id], separators=(",", ":"))
    return "cover-" + hashlib.sha256(encoded.encode()).hexdigest()[:40]


def _origin(facts: FrameFacts, layer: Layer) -> LayerOrigin | None:
    return facts.origins.get(layer.assignment_id)


def joins(facts: FrameFacts, showing: Layer, at: float) -> Layer | None:
    """Skip's next photo: the Frame's next regular media layer of the showing layer's Run after
    it, not yet fully in at `at`, that is not an ending, not the photo left and not on the Never
    show list. None when the offer holds none (a turn past the plan's reach, or the next turn is
    another Run's)."""
    left = photo_of(showing)
    for layer in sorted(facts.layers, key=lambda each: (each.start, each.assignment_id)):
        origin = _origin(facts, layer)
        if (layer.run_id == showing.run_id and not layer.action and layer.presentation == "media"
                and layer.start > showing.start and layer.start + layer.fade_in > at
                and not (origin is not None and origin.ending)
                and photo_of(layer) != left and photo_of(layer) not in facts.listed):
            return layer
    return None


def _next_regular(facts: FrameFacts, showing: Layer, at: float) -> Layer | None:
    """The Frame's next regular layer after the one showing, of any Run, not yet fully in at `at`:
    where a Previous's cover gives way."""
    found = [layer for layer in facts.layers if not layer.action and layer.start > showing.start
             and layer.start + layer.fade_in > at]
    return min(found, key=lambda layer: (layer.start, layer.assignment_id), default=None)


def previous_chain(facts: FrameFacts, showing: Layer, now: float) -> tuple[str | None, int]:
    """Where a Previous steps back from and how far: while the Frame's newest cover is a
    Previous still drawn, one further than it from its anchor; else one back from the photo
    showing now (any other photo presented on the Frame ends the chain)."""
    newest = facts.recent[0] if facts.recent else None
    if newest is not None and newest.cause is CoverCause.PREVIOUS and newest.end > now:
        return newest.anchor, newest.steps + 1
    return photo_of(showing), 1


def previous_step(history: Sequence[ShownPhoto], anchor: str | None, steps: int,
                  usable: Callable[[str], bool], *, passed_over: frozenset[str] = frozenset(),
                  ) -> str | None:
    """The photo `steps` back in a Frame's shown history (newest first) from its newest showing of
    `anchor` (from the top when it has none), each photo counted once per run of showings, the
    rows of `passed_over` (earlier Previous covers) left out and a photo `usable` refuses (no longer
    fits, gone, on the Never show list) passed over. None when the history has nothing that far
    back."""
    entries = [entry for entry in history if entry.assignment_id not in passed_over]
    start = next((index + 1 for index, entry in enumerate(entries) if entry.asset_id == anchor), 0)
    counted, last = 0, anchor
    for entry in entries[start:]:
        if entry.asset_id == last or entry.asset_id == anchor:
            continue
        last = entry.asset_id
        if not usable(entry.asset_id):
            continue
        counted += 1
        if counted == steps:
            return entry.asset_id
    return None


def passed_over(facts: FrameFacts) -> frozenset[str]:
    """The shown-history rows a Previous never steps back onto: its own earlier covers."""
    return frozenset(cover.cover_id for cover in facts.recent if cover.cause is CoverCause.PREVIOUS)


def stays(facts: FrameFacts, kind: SkipKind, at: float, *,
          usable: Callable[[str], bool] | None = None, now: float | None = None) -> StayReason | None:
    """Why the Frame stays when `kind` is asked of it at the shared instant `at`, first match wins
    (decision 0036's `StayReason` table); None when it moves. `usable`: whether a photo may still
    be stepped back onto (Previous; None: any photo not on the Never show list)."""
    if not facts.set_up:
        return StayReason.NOT_SET_UP
    if not facts.answering:
        return StayReason.NOT_ANSWERING
    if facts.resting:
        return StayReason.RESTING
    if facts.paused:
        return StayReason.PAUSED
    showing = showing_at(facts.layers, at)
    if showing is None:
        return StayReason.NOTHING_TO_SKIP
    origin = _origin(facts, showing)
    if showing.presentation == "black" or (origin is not None and origin.ending):
        return StayReason.BLACK
    if origin is not None and origin.hand_placed:
        return StayReason.HAND_PLACED
    if kind is SkipKind.PREVIOUS and _previous_photo(facts, showing, at if now is None else now,
                                                     usable) is None:
        return StayReason.NO_PREVIOUS
    return None


def _previous_photo(facts: FrameFacts, showing: Layer, now: float,
                    usable: Callable[[str], bool] | None) -> str | None:
    anchor, steps = previous_chain(facts, showing, now)
    allowed = usable if usable is not None else (lambda asset_id: True)
    return previous_step(facts.history, anchor, steps,
                         lambda asset_id: asset_id not in facts.listed and allowed(asset_id),
                         passed_over=passed_over(facts))


def _fades(start: float, end: float, fade_in: float, fade_out: float) -> tuple[float, float]:
    """Fades that fit the span: the fade-out first, then the fade-in in what is left."""
    span = end - start
    fade_out = min(fade_out, span)
    return min(fade_in, span - fade_out), fade_out


def cover_for(facts: FrameFacts, kind: SkipKind, cause: CoverCause, *, request_id: str, at: float,
              now: float, cue: str | None, usable: Callable[[str], bool] | None = None) -> Cover | None:
    """The cover a Frame that moves (`stays` is None) gets: a layer above its layers from `at`.
    Next: its next photo whole (`joins`), else a pick from its Scene line ending with the layer it
    covers. Previous: its photo pinned, ending where the Frame's next regular layer is fully in.
    None when the Frame stays."""
    if stays(facts, kind, at, usable=usable, now=now) is not None:
        return None
    showing = showing_at(facts.layers, at)
    assert showing is not None
    origin = _origin(facts, showing) or next(
        (found for layer in facts.layers if layer.run_id == showing.run_id
         and (found := _origin(facts, layer)) is not None), None)
    transition = 0.0 if origin is None else origin.transition
    common = dict(cover_id=cover_id(request_id, facts.frame_id), frame_id=facts.frame_id, cause=cause,
                  covered=showing.assignment_id, run_id=showing.run_id, output_id=showing.output_id,
                  binding_generation=showing.binding_generation, priority=showing.priority,
                  root_order=showing.root_order, admission_order=showing.admission_order, start=at,
                  cue=cue, left=photo_of(showing))
    if kind is SkipKind.PREVIOUS:
        anchor, steps = previous_chain(facts, showing, now)
        pinned = _previous_photo(facts, showing, now, usable)
        following = _next_regular(facts, showing, at)
        end, fade_out = ((following.start + following.fade_in, following.fade_in)
                         if following is not None else (showing.drawn_end, showing.fade_out))
        fade_in, fade_out = _fades(at, end, transition, fade_out)
        return Cover(**common, how="pinned", end=end, fade_in=fade_in, fade_out=fade_out,
                     after_end=showing.after_end, pinned=pinned, anchor=anchor, steps=steps)
    joined = joins(facts, showing, at)
    if joined is None:
        fade_in, fade_out = _fades(at, showing.drawn_end, transition, showing.fade_out)
        return Cover(**common, how="pick", end=showing.drawn_end, fade_in=fade_in, fade_out=fade_out,
                     after_end=showing.after_end)
    end = joined.start + joined.fade_in
    fade_in, _ = _fades(at, end, transition, 0.0)
    layer = joined.model_copy(update=dict(
        assignment_id=common["cover_id"], run_id=showing.run_id, output_id=showing.output_id,
        frame_id=facts.frame_id, binding_generation=showing.binding_generation,
        start=at, end=end, media_origin=at, priority=showing.priority, root_order=showing.root_order,
        admission_order=showing.admission_order, fade_in=fade_in, fade_out=0.0,
        awaits_grant=cue is not None, action=True))
    return Cover(**common, how="joins", end=end, fade_in=fade_in, fade_out=0.0,
                 after_end=joined.after_end, layer=Layer.model_validate(layer.model_dump()),
                 joined=joined.assignment_id)


def pause_refusal(facts: FrameFacts, at: float) -> PauseRefusal | None:
    """Why the Frame is not paused when Pause is asked of it at the shared instant `at`, first
    match wins (decision 0036's `PauseRefusal`); None when it is paused. The one home of who may
    pause: Frame status calls it for `can` and Coordination's command again under its lock."""
    if not facts.set_up:
        return PauseRefusal.NOT_SET_UP
    if not facts.answering:
        return PauseRefusal.NOT_ANSWERING
    if facts.resting:
        return PauseRefusal.RESTING
    if facts.paused:
        return PauseRefusal.PAUSED
    showing = showing_at(facts.layers, at)
    origin = None if showing is None else _origin(facts, showing)
    if (showing is None or showing.presentation == "black" or showing.variant is None
            or (origin is not None and origin.ending)):
        return PauseRefusal.NOTHING_TO_HOLD
    if showing.variant.media_type == "video/mp4":
        return PauseRefusal.VIDEO
    return None


def hold_id(pause_id: str, frame_id: str) -> str:
    """A hold's assignment id: one per pause and Frame."""
    encoded = json.dumps([pause_id, frame_id], separators=(",", ":"))
    return "hold-" + hashlib.sha256(encoded.encode()).hexdigest()[:40]


def pause_run_id(pause_id: str) -> str:
    """The Run a pause's holds name (never a Runtime Run, never a continuation's candidate)."""
    return "pause-" + hashlib.sha256(pause_id.encode()).hexdigest()[:40]


def hold_for(facts: FrameFacts, *, pause_id: str, at: float, ends_at: float, priority: int) -> Layer:
    """The hold a Frame that is paused (`pause_refusal` is None) gets: a layer of the photo it
    shows at `at` (its variant, crop and media origin), above every Run (`priority`, the caller's
    rank above every Run's), from `at` to `ends_at`, an action layer with no fade-in (the same
    picture: no seam) and a fade-out of its Scene's Transition seconds, so its end reveals the
    schedule beneath with the usual fade. It never changes an offered layer."""
    showing = showing_at(facts.layers, at)
    assert showing is not None and showing.variant is not None
    origin = _origin(facts, showing)
    transition = 0.0 if origin is None else origin.transition
    return Layer(
        assignment_id=hold_id(pause_id, facts.frame_id), run_id=pause_run_id(pause_id),
        output_id=showing.output_id, frame_id=facts.frame_id,
        binding_generation=showing.binding_generation, start=at, end=ends_at,
        media_origin=showing.media_origin, priority=priority, variant=showing.variant,
        crop=showing.crop, opacity=showing.opacity, fade_in=0.0,
        fade_out=min(transition, ends_at - at), action=True)


def carried_on(held: Layer, now: float) -> Layer | None:
    """A hold under Carry on at `now`: cut `END_LEAD` and its fade-out after `now`, so the
    schedule beneath shows at once with the usual fade; unchanged when it ends sooner by itself or
    carries an earlier cut (a cut is only ever brought earlier); None when it would not have begun
    by then (it is withdrawn, never drawn)."""
    cut = now + END_LEAD + held.fade_out
    if cut >= held.drawn_end:
        return held
    if cut <= held.start:
        return None
    return Layer.model_validate({**held.model_dump(), "cut_at": cut,
                                 "fade_out": min(held.fade_out, cut - held.start)})


def asked_state(at: float, presented_at: float | None, now: float) -> AskState:
    """Where an asked action stands: `done` once its layer was reported presented, `late` when
    not by `at + ACK_LATE_SECONDS`, else `requested`."""
    if presented_at is not None:
        return AskState.DONE
    return AskState.LATE if now > at + ACK_LATE_SECONDS else AskState.REQUESTED


def asked_until(at: float, end: float) -> float:
    """Until when an action asked at `at`, whose layer is drawn until `end`, is reported: while its
    layer lasts, and never before it has read `late` for `ACK_LATE_KEPT_SECONDS` (`asked_state`),
    so an action whose layer ends sooner than it can be late still shows as late, with its card."""
    return max(end, at + ACK_LATE_SECONDS + ACK_LATE_KEPT_SECONDS)


__all__ = ["asked_state", "asked_until", "carried_on", "cover_for", "cover_id", "hold_for", "hold_id", "joins", "passed_over",
           "pause_refusal", "pause_run_id", "photo_of", "previous_chain", "previous_step", "showing_at", "stays",
           "the_shared_instant"]
