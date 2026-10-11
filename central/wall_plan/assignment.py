"""The wall assignment: every Frame's photos over the plan horizon, computed together in one pure
call (roadmap 5a design rules 1 and 3; run ledger .claude/runs/photo-5a.md, slice W1).

Owner, 2026-10-11: "Frames are not running independently, and it’s up to Central to “plan” where
the photos are assigned. I suggest an algorithm that computes all frame+photo assignments well in
advance"; "Correct. One photo per frame."; "Never show the same photo on two frames at the same
time"; "this “no repeat” idea is secondary to always ensuring that frames have content".

The walk (prior art: rolling-horizon scheduling; greedy on a transversal matroid with Kuhn's
augmenting paths; Kuhn-Munkres for the placement):
1. Fixed showings (offered or secured layers, and each Frame's kept still) are inputs; their
   extents are recorded first. Hand-placed slots take the first of their authored photos that
   fits and is free; their photos are skipped by every Scene line in this plan.
2. Slots are walked by start instant, earliest first. At one instant, each slot offers the first
   `depth` = (slots at this instant) + `limits.offer_extra` photos of its Scene's line
   (`SceneLine.ranked`) that it can take (`Slot` docstring); a photo it could take but for a
   missing measurement or (inside the preparation window) a missing variant is recorded as a
   request instead, at most `depth` of those per slot.
3. The offers' union is walked in rank order (a photo's rank is its smallest position in any
   offering slot's list; ties by asset id); a photo is kept while the kept set still has a
   matching giving each kept photo its own slot (an augmenting path exists).
4. The kept photos are placed on the slots by least total `crop_share` (a minimum-cost perfect
   matching of the kept set; ties by slot id then asset id). Each placement is picked in its
   Scene's line, its crop is the fit rule's, and its extent is recorded.
5. A slot left without a photo takes its Frame's previous photo again when that photo still fits
   and is usable (a repeat on the same Frame never breaks rule 1); otherwise it is unfilled.
Rule 1 never relaxes: a photo whose extent on another Frame overlaps the slot's is never offered,
including a hand-placed one. A kept still's extent stays open until a later FILLED slot on its
Frame replaces what the Frame keeps (`contracts.on_screen.replaces_kept_at`).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field

from central.catalog import Candidate
from central.wall_plan.order import KeptStill, SceneLine
from contracts.models import (
    AfterEnd,
    Fraction,
    FrameProfile,
    Identifier,
    Instant,
    Model,
    PhotoCrop,
)


class PlanWindow(Model):
    """`now` <= `preparation_end` <= `plan_end`. A slot starting before `preparation_end` is
    offered to its Player now, so it takes only photos whose prepared copy is ready; a slot
    after it is tentative and may take a measured photo not yet prepared."""

    now: Instant
    preparation_end: Instant
    plan_end: Instant


class WallLimits(Model):
    offer_extra: int = Field(default=4, ge=0, le=64)
    max_measurements: int = Field(default=64, ge=0)
    max_preparations: int = Field(default=256, ge=0)
    max_slots: int = Field(default=8192, ge=1)


class Slot(Model):
    """One Frame's showing to fill: an Intent of the Runtime's timeline.

    It can take a candidate when: the candidate is in its pool (`pools[slot.pool]`), or in
    `authored` for a hand-placed slot; the fit rule accepts it on `profile` at `crop_limit`
    (`central.wall_plan.fit.judge`, measured key content for an image); it is not
    `preparation_failure`d nor measure-failed; inside the preparation window its variant is usable
    for the profile; and no extent of that photo on another Frame overlaps this slot's."""

    slot_id: Identifier                      # the assignment id
    frame_id: Identifier
    scene_id: str = Field(min_length=1, max_length=160)
    start: Instant
    end: Instant
    fade_in: float = Field(ge=0, allow_inf_nan=False)
    after_end: AfterEnd
    profile: FrameProfile
    crop_limit: Fraction
    pool: str = ""                           # key into `pools`; "" for a hand-placed slot
    authored: tuple[Identifier, ...] = ()    # hand-placed photos, in authored order


class FixedShowing(Model):
    """A layer whose content is already decided (an offered or secured lock). `asset_id` None:
    a lock whose photo the history does not name (a black layer, or a lock offered before 5a);
    it still replaces what its Frame keeps."""

    slot_id: Identifier
    frame_id: Identifier
    asset_id: Identifier | None
    start: Instant
    end: Instant
    fade_in: float = Field(ge=0, allow_inf_nan=False)
    after_end: AfterEnd


class Placed(Model):
    slot_id: Identifier
    asset_id: Identifier
    crop: PhotoCrop
    pass_number: int | None = Field(default=None, ge=1)    # None for a hand-placed slot
    repeat: bool = False      # step 5: the Frame's previous photo again
    tentative: bool = False   # after the preparation window, its variant not ready yet


UnfilledReason = Literal[
    "no_photo_fits",        # nothing in the slot's pool fits its Frame
    "no_photo_free",        # photos fit, but each is on screen elsewhere, unmeasured or unprepared
    "authored_no_eligible_alternative",   # no hand-placed photo fits (as before 5a)
]


class Unfilled(Model):
    slot_id: Identifier
    reason: UnfilledReason


class PrepareNeed(Model):
    asset_id: Identifier
    slot_ids: tuple[Identifier, ...]
    earliest_start: Instant


class WallPlan(Model):
    placed: tuple[Placed, ...] = ()
    unfilled: tuple[Unfilled, ...] = ()
    measure: tuple[Identifier, ...] = ()        # in rank order, at most limits.max_measurements
    prepare: tuple[PrepareNeed, ...] = ()       # at most limits.max_preparations


def plan_wall(slots: Sequence[Slot], fixed: Sequence[FixedShowing], kept: Sequence[KeptStill],
              candidates: Mapping[str, Candidate], lines: Mapping[str, SceneLine],
              pools: Mapping[str, frozenset[str]], *, window: PlanWindow,
              max_clock_uncertainty: float, limits: WallLimits = WallLimits()) -> WallPlan:
    """The wall assignment over `window` (module docstring). `candidates` are hydrated (variant,
    preparation failure, analysis); `lines` are keyed by scene id and mutated through `pick`;
    `pools` maps each slot's pool key to the asset ids its contribution's Sources hold.
    Deterministic for equal inputs. Never changes a fixed showing. ValueError on a slot whose
    scene has no line, or more than `limits.max_slots` slots."""
    raise NotImplementedError
