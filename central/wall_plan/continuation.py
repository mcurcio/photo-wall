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
    raise NotImplementedError


def held_reservations(plan: Plan, *, guard: float) -> tuple[Reservation, ...]:
    """What `plan`'s continuations hold: one Reservation per listed photo, on the Frame its Output
    is bound to, from its continuation's `start - lead - guard` to `until + guard` (open when None).
    Pure; the caller decides which of a Player's offers still hold."""
    raise NotImplementedError


__all__ = ["ContinuationSegment", "FrameExtent", "ListedPhoto", "continuation_lists",
           "held_reservations"]
