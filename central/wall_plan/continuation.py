"""Each Frame's continuation lists, and the photos they hold (decision 0032, slice B): the owner of
"never a photo another Frame uses" past a plan's layers.

Owner, 2026-11-15 (Q1, guidance): when cut off from Central a Frame cycles "its own upcoming photos
from Central's plan, as many as it has downloaded, at the Scene's pace, never a photo another Frame
is using". Owner, 2026-10-11: "Never show the same photo on two frames at the same time".

Two rules, one home each:
- **The lists** (`continuation_lists`): a segment's list is its Run's offered photos on its Output,
  in start order, less any photo that may be on screen on another Frame over the segment's span
  (an extent of this pass's walk, a fixed showing, a reservation, or an earlier segment's list).
  Segments are walked by (start, frame id, run id), so every list of one pass is disjoint from every
  other.
- **What an offer holds** (`held_reservations`): every listed photo of a plan's continuations is
  reserved on its Frame from its continuation's first slot to its `until` (open when None), so a
  later pass never gives it to another Frame while the Player may still be cycling it.

Which offers still hold is coordination's (central/coordination.py: every offer whose revision is
at or past the one the Player's latest readiness names); what a slot shows is
contracts/continuation.py's. Pure: no database, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field

from central.wall_plan.assignment import Reservation
from contracts.models import Continuation, Identifier, Instant, Model, Plan, Positive
from contracts.on_screen import Extent


class ListedPhoto(Model):
    """One offered media layer a segment may list: its assignment id and the photo it shows."""

    assignment_id: Identifier
    asset_id: Identifier


class ContinuationSegment(Model):
    """One Run on top of one Frame past the layers its Player's plan holds there, as the Runtime's
    timeline gives it within the plan horizon: its first turn not offered (`start`), its Scene's
    pace and steady-state Transition, and the next change on that Frame (`until`, None past the
    plan horizon). `candidates`: the Run's offered media layers on this Output, in start order."""

    player_id: Identifier
    frame_id: Identifier
    output_id: Identifier
    run_id: Identifier
    start: Instant
    seconds: Positive
    lead: float = Field(ge=0, allow_inf_nan=False)
    fade_in: float = Field(ge=0, allow_inf_nan=False)
    fade_out: float = Field(ge=0, allow_inf_nan=False)
    until: Instant | None = None
    candidates: tuple[ListedPhoto, ...]


class FrameExtent(Model):
    """Where one photo may be on screen: on `frame_id` over `extent` (guards included)."""

    frame_id: Identifier
    extent: Extent


def continuation_lists(segments: Sequence[ContinuationSegment], *,
                       extents: Mapping[str, Sequence[FrameExtent]],
                       guard: Mapping[str, float]) -> dict[str, tuple[Continuation, ...]]:
    """Each Player's continuations, by player id. `extents` maps an asset id to every place this
    pass knows it may be on screen (the walk's placed slots and fixed showings, and `reserved`);
    `guard` is each Frame's hand-over guard in seconds by frame id (wall plan rule 1's). A candidate
    is listed when no extent of its photo on another Frame overlaps
    [start - lead - guard, until + guard) (open when `until` is None) and no earlier segment on
    another Frame listed it. A segment left with no photo gives no continuation. Deterministic for
    equal inputs. ValueError for two segments on one Output that overlap."""
    by_output: dict[tuple[str, str], list[ContinuationSegment]] = {}
    for segment in segments:
        by_output.setdefault((segment.player_id, segment.output_id), []).append(segment)
    for on_output in by_output.values():
        on_output.sort(key=lambda segment: segment.start)
        for earlier, later in zip(on_output, on_output[1:]):
            if earlier.until is None or later.start - later.lead < earlier.until:
                raise ValueError("segments overlap")
    listed: dict[str, list[FrameExtent]] = {}
    found: dict[str, list[Continuation]] = {}
    for segment in sorted(segments, key=lambda s: (s.start, s.frame_id, s.run_id)):
        span = _span(segment, guard.get(segment.frame_id, 0.0))
        photos = tuple(
            photo.assignment_id for photo in segment.candidates
            if not _elsewhere(segment.frame_id, span, extents.get(photo.asset_id, ()))
            and not _elsewhere(segment.frame_id, span, listed.get(photo.asset_id, ())))
        if not photos:
            continue
        for photo in segment.candidates:
            if photo.assignment_id in photos:
                listed.setdefault(photo.asset_id, []).append(
                    FrameExtent(frame_id=segment.frame_id, extent=span))
        found.setdefault(segment.player_id, []).append(Continuation(
            output_id=segment.output_id, run_id=segment.run_id, photos=photos,
            start=segment.start, seconds=segment.seconds, lead=segment.lead,
            fade_in=segment.fade_in, fade_out=segment.fade_out, until=segment.until))
    return {player: tuple(sorted(continuations, key=lambda c: (c.output_id, c.start)))
            for player, continuations in sorted(found.items())}


def _span(segment: ContinuationSegment, guard: float) -> Extent:
    """The segment's span, its guard included: [start - lead - guard, until + guard)."""
    return Extent(start=segment.start - segment.lead - guard,
                  end=None if segment.until is None else segment.until + guard)


def _elsewhere(frame_id: str, span: Extent, extents: Sequence[FrameExtent]) -> bool:
    return any(other.frame_id != frame_id and other.extent.overlaps(span) for other in extents)


def held_reservations(plan: Plan, *, guard: float) -> tuple[Reservation, ...]:
    """What `plan`'s continuations hold: one Reservation per listed photo, on the Frame its Output
    is bound to, from its continuation's `start - lead - guard` to `until + guard` (open when None).
    Pure; the caller decides which of a Player's offers still hold."""
    frames = {binding.output_id: binding.frame_id for binding in plan.bindings}
    layers = {layer.assignment_id: layer for layer in plan.layers}
    held: list[Reservation] = []
    for continuation in plan.continuations:
        extent = Extent(start=continuation.start - continuation.lead - guard,
                        end=None if continuation.until is None else continuation.until + guard)
        for photo in continuation.photos:
            variant = layers[photo].variant
            if variant is not None:
                held.append(Reservation(frame_id=frames[continuation.output_id],
                                        asset_id=variant.asset_id, extent=extent))
    return tuple(held)


__all__ = ["ContinuationSegment", "FrameExtent", "ListedPhoto", "continuation_lists",
           "held_reservations"]
