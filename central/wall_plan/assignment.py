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
   and is usable (a repeat on the same Frame never breaks rule 1). Failing that, content first
   (owner, 2026-10-11: no-repeat and the like are "secondary to always ensuring that frames have
   content"): the line's first photo of its pool whose key content is not known (not measured
   yet, measured at no current analysis, or its measurement failed) that passes the size-only
   part of the fit rule (`shape_fit`), is usable and is free, with the centred crop; inside the
   preparation window an unprepared one is asked to be prepared instead. Otherwise unfilled.
Rule 1 never relaxes: a photo whose extent on another Frame overlaps the slot's is never offered,
including a hand-placed one. A kept still's extent stays open until a later FILLED slot on its
Frame replaces what the Frame keeps (`contracts.on_screen.replaces_kept_at`).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import groupby
from typing import Literal

from pydantic import Field

from central.catalog import Candidate
from central.wall_plan.fit import (
    LARGE_FRAME_INCHES,
    LARGE_FRAME_VIDEO_SHORT_SIDE,
    Fits,
    FitVerdict,
    MediaKind,
    crop_share,
    judge,
    shape_fit,
)
from central.wall_plan.key_content import KeyContent, Measured, MeasureFailed
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
from contracts.on_screen import Extent, on_screen_extent, replaces_kept_at
from contracts.photo_crop import CENTRED


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
    a black layer, or a lock whose photo neither the history nor any candidate's prepared copy
    names (the Planner's `_lock_photos`); it still replaces what its Frame keeps."""

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
    Deterministic for equal inputs. Never changes a fixed showing. ValueError on a Scene-line
    slot whose scene has no line (a hand-placed slot needs none), or more than
    `limits.max_slots` slots."""
    if len(slots) > limits.max_slots:
        raise ValueError(f"{len(slots)} slots exceed the wall plan's {limits.max_slots}")
    for slot in slots:
        if slot.pool and slot.scene_id not in lines:
            raise ValueError(f"slot {slot.slot_id} names scene {slot.scene_id!r}, which has no line")
    return _Walk(candidates, lines, pools, window=window, uncertainty=max_clock_uncertainty,
                 limits=limits).run(slots, fixed, kept)


def layer_after_end(after_end: AfterEnd, kind: MediaKind) -> AfterEnd:
    """What a layer showing a `kind` keeps after its end: the authored after-state, except that
    only a still is kept (a kept Scene's videos change nothing). One home: the Planner's layers
    and this module's extents both ask here, so a planned extent is the layer's own."""
    if after_end == "keep_this_photo" and kind != "image":
        return "leave_as_is"
    return after_end


def variant_usable(candidate: Candidate, profile: FrameProfile) -> bool:
    """Whether the candidate's prepared copy can be drawn on a Frame of `profile`: its media type
    and duration, and requirements :186's floor for a video on a large Frame. Landscape or
    portrait is not a test (the fit rule judges the original's ratio; owner 2026-10-11, "It's about the
    ratio, not strict labels")."""
    variant = candidate.variant
    if variant is None:
        return False
    if candidate.kind == "image":
        return variant.media_type in ("image/jpeg", "image/png") and variant.duration is None
    return (
        variant.media_type == "video/mp4" and variant.duration is not None
        and (profile.diagonal_inches < LARGE_FRAME_INCHES
             or min(variant.width, variant.height) >= LARGE_FRAME_VIDEO_SHORT_SIDE)
    )


def fit_of(candidate: Candidate, profile: FrameProfile, crop_limit: float) -> FitVerdict | None:
    """The photo fit rule's verdict for a hydrated candidate on `profile` at `crop_limit`
    (central/wall_plan/fit.py). None: an image that passes on its size alone and whose key
    content is not measured yet. Memoised: `judge` is pure, so the memo is keyed by every input
    and never disagrees with it; an empty memo gives the same answers."""
    refused = shape_fit(candidate.kind, candidate.original_width, candidate.original_height,
                        profile, crop_limit)
    if refused is not None:
        return refused
    analysis = candidate.analysis
    key = analysis.key if isinstance(analysis, Measured) else None
    if candidate.kind == "image" and key is None:
        return None
    return _judged(candidate.kind, candidate.original_width, candidate.original_height, key,
                   profile, crop_limit)


def fits_slot(candidate: Candidate, slot: Slot) -> bool:
    """Whether `candidate` could fill `slot` on its own merits, apart from where else it is on
    screen: measured, it fits, it has not failed, and a prepared copy (if any) is usable. A
    Scene's line counts these as pickable (a pass ends once each is picked)."""
    return _standing(candidate, slot.profile, slot.crop_limit, inside=False)[0] == "take"


@lru_cache(maxsize=65536)
def _judged(kind: MediaKind, width: int, height: int, key: KeyContent | None,
            profile: FrameProfile, crop_limit: float) -> FitVerdict:
    return judge(kind, width, height, key, profile, crop_limit)


_Standing = Literal["take", "measure", "prepare", "unusable", "no"]


def _standing(candidate: Candidate, profile: FrameProfile, crop_limit: float, *,
              inside: bool) -> tuple[_Standing, Fits | None]:
    """What a slot does with one candidate of its pool, before asking where else it is on
    screen: take it (with the fit rule's crop), request a measurement or (inside the
    preparation window) a prepared copy, leave it ("unusable": it fits, but its prepared copy
    cannot be drawn here), or nothing (it does not fit, or it failed)."""
    if candidate.preparation_failure or isinstance(candidate.analysis, MeasureFailed):
        return "no", None
    verdict = fit_of(candidate, profile, crop_limit)
    if verdict is None:
        return "measure", None
    if not isinstance(verdict, Fits):
        return "no", None
    if candidate.variant is None:
        return ("prepare" if inside else "take"), verdict
    return ("take" if variant_usable(candidate, profile) else "unusable"), verdict


_Offer = tuple[str, _Standing, Fits | None]


@dataclass(frozen=True, slots=True, eq=False)
class _Showing:
    """A decided showing on one Frame (a fixed layer or a placement); `contracts.on_screen`'s
    `Showing`. Compared by identity, as `on_screen_extent` skips the showing itself."""

    start: float
    end: float
    fade_in: float
    after_end: AfterEnd
    asset_id: str | None


@dataclass(slots=True)
class _Wall:
    """Every decided showing by Frame and by photo, and each Frame's kept still from the
    history. Extents are computed from what is decided so far: a later decision on a Frame can
    only end a kept still sooner, so an extent never grows and a free answer stays true."""

    uncertainty: float
    on_frame: dict[str, list[_Showing]] = field(default_factory=dict)
    of_asset: dict[str, list[tuple[str, _Showing]]] = field(default_factory=dict)
    kept: dict[str, list[KeptStill]] = field(default_factory=dict)
    kept_on: dict[str, KeptStill] = field(default_factory=dict)
    # Each decided showing's extent, by Frame and by the showing's identity: valid until the
    # next showing on that Frame is decided (`add` drops the Frame's entries), since only a
    # later showing on its own Frame changes an extent. Decided showings stay referenced in
    # `on_frame`, so an identity is never reused while its entry exists.
    _extents: dict[str, dict[int, Extent]] = field(default_factory=dict)

    def add(self, frame_id: str, showing: _Showing) -> None:
        self.on_frame.setdefault(frame_id, []).append(showing)
        self._extents.pop(frame_id, None)
        if showing.asset_id is not None:
            self.of_asset.setdefault(showing.asset_id, []).append((frame_id, showing))

    def add_kept(self, still: KeptStill) -> None:
        self.kept.setdefault(still.asset_id, []).append(still)
        self.kept_on[still.frame_id] = still

    def extent(self, frame_id: str, showing: _Showing) -> Extent:
        return on_screen_extent(showing, self.on_frame.get(frame_id, ()), self.uncertainty)

    def kept_extent(self, still: KeptStill) -> Extent | None:
        """A kept still from the history holds from `since` until a showing on its Frame
        replaces what the Frame keeps at or after it (`on_screen_extent`'s rule for a kept
        layer, with the guard)."""
        guard = 2 * self.uncertainty
        replaced = [at for showing in self.on_frame.get(still.frame_id, ())
                    if (at := replaces_kept_at(showing)) is not None and at >= still.since]
        start = still.since - guard
        if not replaced:
            return Extent(start=start)
        end = max(still.since, min(replaced)) + guard
        return Extent(start=start, end=end) if end > start else None

    def decided_extent(self, frame_id: str, showing: _Showing) -> Extent:
        """`extent` of a showing already decided on `frame_id` (memoised; see `_extents`)."""
        memo = self._extents.setdefault(frame_id, {})
        found = memo.get(id(showing))
        if found is None:
            found = memo[id(showing)] = self.extent(frame_id, showing)
        return found

    def free(self, asset_id: str, frame_id: str, extent: Extent) -> bool:
        """Rule 1: no extent of `asset_id` on another Frame overlaps `extent`."""
        guard = 2 * self.uncertainty
        for other, showing in self.of_asset.get(asset_id, ()):
            if other == frame_id:
                continue
            # Every extent starts at its showing's start less the guard: one that starts at or
            # after `extent` ends cannot overlap it (a cheap test before the extent itself).
            if extent.end is not None and showing.start - guard >= extent.end:
                continue
            if self.decided_extent(other, showing).overlaps(extent):
                return False
        for still in self.kept.get(asset_id, ()):
            if still.frame_id != frame_id:
                held = self.kept_extent(still)
                if held is not None and held.overlaps(extent):
                    return False
        return True

    def previous(self, frame_id: str, before: float) -> str | None:
        """The Frame's latest decided photo starting before `before`, else its kept still."""
        latest: _Showing | None = None
        for showing in self.on_frame.get(frame_id, ()):
            if (showing.asset_id is not None and showing.start < before
                    and (latest is None or showing.start >= latest.start)):
                latest = showing
        if latest is not None:
            return latest.asset_id
        still = self.kept_on.get(frame_id)
        return still.asset_id if still is not None else None


class _Walk:
    """One `plan_wall` call's state (module docstring, steps 1 to 5)."""

    def __init__(self, candidates: Mapping[str, Candidate], lines: Mapping[str, SceneLine],
                 pools: Mapping[str, frozenset[str]], *, window: PlanWindow, uncertainty: float,
                 limits: WallLimits) -> None:
        self.candidates, self.lines, self.pools = candidates, lines, pools
        self.window, self.limits = window, limits
        self.wall = _Wall(uncertainty)
        self.placed: dict[str, Placed] = {}
        self.unfilled: dict[str, Unfilled] = {}
        self.measure: dict[str, None] = {}
        self.prepare: dict[str, tuple[list[str], float]] = {}
        self.reserved: frozenset[str] = frozenset()
        # Caches within this call. A slot's class is what its standings depend on (its pool,
        # profile, crop limit and side of the preparation window), numbered once per slot so a
        # standing is looked up by (asset id, class) without hashing the profile again.
        self._classes: dict[tuple, int] = {}
        self._class_of: dict[str, int] = {}
        self._standings: dict[tuple[str, int], tuple[_Standing, Fits | None]] = {}
        self._offerable: dict[tuple[str, int], tuple[int, tuple[_Offer, ...]]] = {}
        self._pool_offers: dict[int, dict[str, _Offer]] = {}
        self._ranked: dict[str, tuple[int, tuple[str, ...]]] = {}
        self._picks: dict[str, int] = {}

    def run(self, slots: Sequence[Slot], fixed: Sequence[FixedShowing],
            kept: Sequence[KeptStill]) -> WallPlan:
        for showing in fixed:
            self.wall.add(showing.frame_id, _Showing(showing.start, showing.end, showing.fade_in,
                                                     showing.after_end, showing.asset_id))
        for still in kept:
            self.wall.add_kept(still)
        ordered = sorted(slots, key=lambda slot: (slot.start, slot.slot_id))
        hand_placed = [slot for slot in ordered if not slot.pool]
        self.reserved = frozenset(asset for slot in hand_placed for asset in slot.authored)
        for slot in hand_placed:
            self._hand_placed(slot)
        for _, group in groupby((slot for slot in ordered if slot.pool),
                                key=lambda slot: slot.start):
            self._instant(list(group))
        return WallPlan(
            placed=tuple(self.placed[slot.slot_id] for slot in ordered
                         if slot.slot_id in self.placed),
            unfilled=tuple(self.unfilled[slot.slot_id] for slot in ordered
                           if slot.slot_id in self.unfilled),
            measure=tuple(self.measure),
            prepare=tuple(PrepareNeed(asset_id=asset_id, slot_ids=tuple(slot_ids),
                                      earliest_start=earliest)
                          for asset_id, (slot_ids, earliest) in self.prepare.items()),
        )

    def _hand_placed(self, slot: Slot) -> None:
        """Step 1: the first authored photo that fits and is free."""
        depth = 1 + self.limits.offer_extra
        requested, possible = 0, False
        for asset_id in slot.authored:
            candidate = self.candidates.get(asset_id)
            if candidate is None:
                continue
            standing, verdict = self._standing(candidate, slot)
            if standing == "no":
                continue
            possible = True
            if standing == "unusable" or not self._free(candidate, slot):
                continue
            if standing == "take":
                self._place(slot, candidate, verdict, pass_number=None)
                return
            if requested < depth:
                self._request(standing, asset_id, slot)
                requested += 1
        self.unfilled[slot.slot_id] = Unfilled(
            slot_id=slot.slot_id,
            reason="no_photo_free" if possible else "authored_no_eligible_alternative")

    def _instant(self, group: list[Slot]) -> None:
        """Steps 2 to 5 for the Scene-line slots starting at one instant."""
        depth = len(group) + self.limits.offer_extra
        offers: dict[str, list[str]] = {}
        verdicts: dict[tuple[str, str], Fits] = {}
        possible: set[str] = set()
        for slot in group:
            taken: list[str] = []
            requested = 0
            for asset_id, standing, verdict in self._line_offerable(slot):
                possible.add(slot.slot_id)
                candidate = self.candidates[asset_id]
                if standing == "unusable" or not self._free(candidate, slot):
                    continue
                if standing == "take":
                    taken.append(asset_id)
                    verdicts[(slot.slot_id, asset_id)] = verdict
                    if len(taken) >= depth:
                        break
                elif requested < depth:
                    self._request(standing, asset_id, slot)
                    requested += 1
            offers[slot.slot_id] = taken
        matched = _least_crop(_greedy(offers), offers, verdicts)
        by_id = {slot.slot_id: slot for slot in group}
        for slot_id in sorted(matched):
            slot, asset_id = by_id[slot_id], matched[slot_id]
            self._place(slot, self.candidates[asset_id], verdicts[(slot_id, asset_id)],
                        pass_number=self._pick(slot.scene_id, asset_id))
        for slot in group:
            if slot.slot_id not in matched:
                self._repeat_or_unfilled(slot, possible=slot.slot_id in possible)

    def _repeat_or_unfilled(self, slot: Slot, *, possible: bool) -> None:
        """Step 5, content first: the Frame's previous photo again, when it still fits this
        slot, is usable and is free; else a photo whose key content is unknown (`_unjudged`)."""
        asset_id = self.wall.previous(slot.frame_id, slot.start)
        candidate = self.candidates.get(asset_id) if asset_id is not None else None
        if candidate is not None:
            standing, verdict = self._standing(candidate, slot)
            if standing == "take" and self._free(candidate, slot):
                try:
                    pass_number = self._pick(slot.scene_id, candidate.asset_id)
                except KeyError:   # not in this Scene's line: an earlier Scene's photo
                    pass_number = self.lines[slot.scene_id].pass_number
                self._place(slot, candidate, verdict, pass_number=pass_number, repeat=True)
                return
        if self._unjudged(slot):
            return
        self.unfilled[slot.slot_id] = Unfilled(
            slot_id=slot.slot_id, reason="no_photo_free" if possible else "no_photo_fits")

    def _unjudged(self, slot: Slot) -> bool:
        """Step 5's last resort: place the line's first photo of the slot's pool whose key
        content is unknown, that passes `shape_fit`, is usable and is free, centred. Inside the
        preparation window an unprepared one is requested (at most `depth`) and skipped."""
        pool = self.pools.get(slot.pool, frozenset())
        inside = slot.start < self.window.preparation_end
        requested, depth = 0, 1 + self.limits.offer_extra
        for asset_id in self._line_ranked(slot.scene_id):
            candidate = self.candidates.get(asset_id)
            if (asset_id not in pool or asset_id in self.reserved or candidate is None
                    or candidate.kind != "image" or candidate.preparation_failure
                    or isinstance(candidate.analysis, Measured)
                    or shape_fit("image", candidate.original_width, candidate.original_height,
                                 slot.profile, slot.crop_limit) is not None):
                continue
            if candidate.variant is not None and not variant_usable(candidate, slot.profile):
                continue
            if not self._free(candidate, slot):
                continue
            if inside and candidate.variant is None:
                if requested < depth:
                    self._request("prepare", asset_id, slot)
                    requested += 1
                continue
            self._place(slot, candidate, Fits(
                crop=CENTRED, crop_share=crop_share(candidate.original_width,
                                                    candidate.original_height, slot.profile),
                interest_kept=1.0), pass_number=self._pick(slot.scene_id, asset_id))
            return True
        return False

    def _slot_class(self, slot: Slot) -> int:
        number = self._class_of.get(slot.slot_id)
        if number is None:
            key = (slot.pool, slot.profile, slot.crop_limit,
                   slot.start < self.window.preparation_end)
            number = self._classes.setdefault(key, len(self._classes))
            self._class_of[slot.slot_id] = number
        return number

    def _standing(self, candidate: Candidate, slot: Slot) -> tuple[_Standing, Fits | None]:
        key = (candidate.asset_id, self._slot_class(slot))
        standing = self._standings.get(key)
        if standing is None:
            standing = self._standings[key] = _standing(
                candidate, slot.profile, slot.crop_limit,
                inside=slot.start < self.window.preparation_end)
        return standing

    def _line_offerable(self, slot: Slot) -> tuple[_Offer, ...]:
        """The slot's Scene line (`SceneLine.ranked`) narrowed to the photos of its pool a slot
        of its class could take or request: not hand-placed, and fitting (module docstring,
        step 2). Where else each is on screen is asked by the caller."""
        ranked = self._line_ranked(slot.scene_id)
        version = self._picks.get(slot.scene_id, 0)
        number = self._slot_class(slot)
        cached = self._offerable.get((slot.scene_id, number))
        if cached is None or cached[0] != version:
            offers = self._pool_offers.get(number)
            if offers is None:
                offers = self._pool_offers[number] = {}
                for asset_id in sorted(self.pools.get(slot.pool, frozenset())):
                    candidate = self.candidates.get(asset_id)
                    if asset_id in self.reserved or candidate is None:
                        continue
                    standing, verdict = self._standing(candidate, slot)
                    if standing != "no":
                        offers[asset_id] = (asset_id, standing, verdict)
            cached = (version, tuple(offers[asset_id] for asset_id in ranked
                                     if asset_id in offers))
            self._offerable[(slot.scene_id, number)] = cached
        return cached[1]

    def _free(self, candidate: Candidate, slot: Slot) -> bool:
        showing = _Showing(slot.start, slot.end, slot.fade_in,
                           layer_after_end(slot.after_end, candidate.kind), candidate.asset_id)
        return self.wall.free(candidate.asset_id, slot.frame_id,
                              self.wall.extent(slot.frame_id, showing))

    def _place(self, slot: Slot, candidate: Candidate, verdict: Fits | None, *,
               pass_number: int | None, repeat: bool = False) -> None:
        if verdict is None:
            raise AssertionError("a placement always carries the fit rule's verdict")
        self.wall.add(slot.frame_id, _Showing(
            slot.start, slot.end, slot.fade_in,
            layer_after_end(slot.after_end, candidate.kind), candidate.asset_id))
        self.placed[slot.slot_id] = Placed(
            slot_id=slot.slot_id, asset_id=candidate.asset_id, crop=verdict.crop,
            pass_number=pass_number, repeat=repeat,
            tentative=slot.start >= self.window.preparation_end and candidate.variant is None)

    def _request(self, standing: _Standing, asset_id: str, slot: Slot) -> None:
        if standing == "measure":
            if asset_id not in self.measure and len(self.measure) < self.limits.max_measurements:
                self.measure[asset_id] = None
            return
        need = self.prepare.get(asset_id)
        if need is None:
            if len(self.prepare) < self.limits.max_preparations:
                self.prepare[asset_id] = ([slot.slot_id], slot.start)
            return
        slot_ids, earliest = need
        if slot.slot_id not in slot_ids:
            slot_ids.append(slot.slot_id)
        self.prepare[asset_id] = (slot_ids, min(earliest, slot.start))

    def _line_ranked(self, scene_id: str) -> tuple[str, ...]:
        """`SceneLine.ranked`, read again only after a pick changed the line."""
        version = self._picks.get(scene_id, 0)
        cached = self._ranked.get(scene_id)
        if cached is None or cached[0] != version:
            cached = (version, self.lines[scene_id].ranked())
            self._ranked[scene_id] = cached
        return cached[1]

    def _pick(self, scene_id: str, asset_id: str) -> int:
        pass_number = self.lines[scene_id].pick(asset_id)
        self._picks[scene_id] = self._picks.get(scene_id, 0) + 1
        return pass_number


def _greedy(offers: Mapping[str, Sequence[str]]) -> list[str]:
    """Step 3: the offered photos in rank order (smallest position in any offering slot's list,
    then asset id), each kept while the kept set still has a matching giving every kept photo
    its own slot (Kuhn's augmenting path). Greedy is optimal here: the sets of photos that can
    each have their own slot are the independent sets of a transversal matroid."""
    rank: dict[str, int] = {}
    adjacency: dict[str, list[str]] = {}
    for slot_id in sorted(offers):
        for position, asset_id in enumerate(offers[slot_id]):
            rank[asset_id] = min(rank.get(asset_id, position), position)
            adjacency.setdefault(asset_id, []).append(slot_id)
    holder: dict[str, str] = {}   # slot id -> asset id

    def augment(asset_id: str, seen: set[str]) -> bool:
        for slot_id in adjacency[asset_id]:
            if slot_id in seen:
                continue
            seen.add(slot_id)
            if slot_id not in holder or augment(holder[slot_id], seen):
                holder[slot_id] = asset_id
                return True
        return False

    kept: list[str] = []
    for asset_id in sorted(rank, key=lambda asset: (rank[asset], asset)):
        if len(kept) == len(offers):
            break
        if augment(asset_id, set()):
            kept.append(asset_id)
    return kept


# A cost above any matching of real edges (each crop share is at most 1).
_NO_EDGE = 1e6


def _least_crop(kept: Sequence[str], offers: Mapping[str, Sequence[str]],
                verdicts: Mapping[tuple[str, str], Fits]) -> dict[str, str]:
    """Step 4: every kept photo on a slot that offered it, least total crop share first
    (Kuhn-Munkres; rows by asset id, columns by slot id, so ties fall the same way each time).
    Returns slot id -> asset id."""
    rows = sorted(kept)
    columns = sorted(offers)
    if not rows:
        return {}
    cost = [[verdicts[(slot_id, asset_id)].crop_share if (slot_id, asset_id) in verdicts
             else _NO_EDGE for slot_id in columns] for asset_id in rows]
    return {columns[column]: rows[row] for row, column in enumerate(_hungarian(cost))}


def _hungarian(cost: Sequence[Sequence[float]]) -> list[int]:
    """The minimum-cost assignment of every row to a distinct column (rows <= columns), by
    Kuhn-Munkres with potentials, O(rows^2 x columns). Returns each row's column."""
    rows, columns = len(cost), len(cost[0])
    u = [0.0] * (rows + 1)
    v = [0.0] * (columns + 1)
    owner = [0] * (columns + 1)    # column -> row, 1-based; 0 is none
    way = [0] * (columns + 1)
    for row in range(1, rows + 1):
        owner[0] = row
        column = 0
        least = [math.inf] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[column] = True
            current, delta, nearest = owner[column], math.inf, 0
            for other in range(1, columns + 1):
                if used[other]:
                    continue
                reduced = cost[current - 1][other - 1] - u[current] - v[other]
                if reduced < least[other]:
                    least[other], way[other] = reduced, column
                if least[other] < delta:
                    delta, nearest = least[other], other
            for index in range(columns + 1):
                if used[index]:
                    u[owner[index]] += delta
                    v[index] -= delta
                else:
                    least[index] -= delta
            column = nearest
            if owner[column] == 0:
                break
        while column:
            previous = way[column]
            owner[column] = owner[previous]
            column = previous
    result = [0] * rows
    for column in range(1, columns + 1):
        if owner[column]:
            result[owner[column] - 1] = column - 1
    return result
