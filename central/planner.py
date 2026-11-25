"""Pure bounded rolling media selection; no acquisition or execution authority.

Each contribution's photos are the union of its Sources' whole pools (roadmap 4, C11): no
window and no candidate cap. A pool that has never completed a check still plays what it has
(C7: the last pool keeps playing while Immich is down). Follow-up S3: the Planner reads only the
top of each Scene's line from those pools, fresh every pass (`SceneLines`, `_read_lines`); every
photo stays reachable, as the line ranks it.

Roadmap 5a (run ledger .claude/runs/photo-5a.md, slice W1): every Frame is planned together over
the plan horizon (an hour from coordination) by the wall assignment
(central/wall_plan/assignment.py); only its slots inside the preparation horizon become layers,
the rest is the tentative `Projection.plan`. Owner, 2026-10-11: "I suggest an algorithm that
computes all frame+photo assignments well in advance".
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import Field

from central.catalog import Candidate
from central.everyday.model import SKIP_LEAD, Cover, Pause
from central.house_time.calculator import house_date
from central.house_time.model import HouseTime
from central.installation_models import FrameOutput
from central.runtime import Intent, Runtime, RuntimeBudgetExceeded
from central.wall_plan.assignment import (
    FixedShowing,
    PlanWindow,
    Reservation,
    Slot,
    UnfilledReason,
    WallLimits,
    candidate_shape_fit,
    fits_slot,
    frame_clock_uncertainty,
    layer_after_end,
    plan_wall,
    shape_bounds,
    useful_for,
    variant_usable,
)
from central.wall_plan.continuation import (
    ContinuationSegment,
    FrameExtent,
    ListedPhoto,
    continuation_lists,
)
from central.wall_plan.covers import cover_start, joined_layer, layer_fields, still_due
from central.wall_plan.fit import FitTarget, fit_target
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.order import (
    LineAnswer,
    LineMember,
    LineQuery,
    SceneLine,
    SceneLines,
    ScenePass,
    SourceLine,
    WallHistory,
    last_shown_at,
    recent_assets,
    scene_passes,
)
from central.wall_plan.selection import crop_limit_of
from contracts.models import (
    AfterEnd,
    Continuation,
    Identifier,
    Instant,
    Layer,
    Model,
    OutputBinding,
    PhotoCrop,
    UnfilledSpan,
    stack_order,
)


class PlanningError(ValueError):
    """The caller's input is inconsistent (a Frame bound twice, a pool keyed by another Source, a
    non-finite time). Raised within one Intent's `add`, it refuses that Intent alone
    (`intent_refused`); no budget raises it: a budget shortens the plan (`plan_shortened`)."""


# Roadmap 5a: how far ahead the wall plan reaches, every Frame together (owner, 2026-10-11: "an
# algorithm that computes all frame+photo assignments well in advance"; the plan horizon's 1 h
# default confirmed on the r2 gate page). Coordination's `plan_seconds` defaults to it.
PLAN_SECONDS = 3600.0


class PlannerLimits(Model):
    """The Planner's work budgets. Each one shortens or truncates what a pass returns, never
    refuses it: Runtime transitions (`max_events`) and layers inside the preparation horizon
    (`max_assignments`) cut the plan at one instant (`plan_shortened`); diagnostics past
    `max_diagnostics` are counted (`Projection.diagnostics_dropped`). Nothing here bounds how long
    a Scene's cycle is, how far ahead the wall plans, or how many Players, bindings or Sources it
    has: the Scene and the coordination limits own those."""

    max_events: int = Field(default=10000, ge=1)
    max_assignments: int = Field(default=4096, ge=1)
    max_acquisitions: int = Field(default=256, ge=1)
    max_diagnostics: int = Field(default=8192, ge=1)
    # Roadmap 5a: measurement requests per projection (central/wall_plan/assignment.py).
    max_measurements: int = Field(default=64, ge=0)
    # Follow-up S3: a Scene's line is read `hydrate_factor` members per slot at a time.
    hydrate_factor: int = Field(default=4, ge=1, le=64)

    @classmethod
    def over(cls, plan_seconds: float, horizon_seconds: float) -> PlannerLimits:
        """The budgets of a wall plan reading `plan_seconds` ahead, re-planned every
        `horizon_seconds` (roadmap 5a): the Runtime's transition budget was sized for one
        preparation horizon, so the plan gets it that many times over."""
        return cls(max_events=cls().max_events * math.ceil(plan_seconds / horizon_seconds))


class AcquisitionRequest(Model):
    asset_id: str
    assignment_ids: tuple[str, ...]
    earliest_start: float


class MeasureRequest(Model):
    """Roadmap 5a: measure this photo's key content (the media worker's measurement task)."""

    asset_id: Identifier


class AssignmentSelection(Model):
    assignment_id: str
    asset_id: str | None = None
    locked: bool = False
    # Roadmap 5a (slice W1 sets them on every new media selection): its Scene and that Scene's
    # pass, which coordination records with the offer (central/wall_plan/ports.py ShownHistory).
    scene_id: str | None = None
    pass_number: int | None = Field(default=None, ge=1)


PlannedState = Literal["locked", "offered", "tentative", "unfilled"]


class PlannedPhoto(Model):
    """Roadmap 5a: one slot of the wall plan over the plan horizon. "locked": an offered or
    secured layer; "offered": a new layer in this projection; "tentative": beyond the preparation
    horizon, not yet a layer; "unfilled": no photo (`reason` says why)."""

    assignment_id: Identifier
    frame_id: Identifier
    start: Instant
    end: Instant
    state: PlannedState
    asset_id: Identifier | None = None
    crop: PhotoCrop | None = None
    reason: str | None = None


class Diagnostic(Model):
    code: str
    target: str | None = None
    assignment_id: str | None = None
    source_ref: str | None = None
    # The Run and Scene of the Intent it is about; `reason` says why for `intent_refused` (the
    # refusal's message) and `plan_shortened` (which budget cut the plan).
    run_id: str | None = None
    scene_id: str | None = None
    reason: str | None = None


class CueMember(Model):
    """One showing inside the preparation horizon that the Planner placed (or left unfilled, or
    on an unbound Frame) and that opens its Run (its layer awaits a grant, decision 0032), for
    coordination's cues (`Coordinator._groups`): a cue is the showings of one root starting to
    show at one instant. A refused or cut showing is never a member, so no cue waits on a layer
    that will not be offered."""

    assignment_id: str
    root_id: str
    target: str
    shows_from: float
    interval_end: float


class PlayerProposal(Model):
    player_id: str
    layers: tuple[Layer, ...]
    # Media Intents inside the preparation horizon that ended the pass with no layer.
    unfilled: tuple[UnfilledSpan, ...] = ()
    # Decision 0032 (slice B): each Run's continuation on each of this Player's Outputs past its
    # layers (central/wall_plan/continuation.py `continuation_lists`).
    continuations: tuple[Continuation, ...] = ()


class Projection(Model):
    now: float
    horizon_end: float
    valid_until: float
    players: tuple[PlayerProposal, ...]
    acquisitions: tuple[AcquisitionRequest, ...]
    diagnostics: tuple[Diagnostic, ...]
    selections: tuple[AssignmentSelection, ...]
    # Roadmap 5a: the measurements this plan needs, and the whole wall plan (slice W1).
    measurements: tuple[MeasureRequest, ...] = ()
    plan: tuple[PlannedPhoto, ...] = ()
    # Diagnostics past `PlannerLimits.max_diagnostics`, counted, not kept.
    diagnostics_dropped: int = 0
    # The instant a budget cut the plan at (`plan_shortened`), None when whole: nothing but a
    # lock starts to show at or after it, and `valid_until` does not reach past it.
    cut: float | None = None
    # The showings coordination's cues group (`CueMember`): the one timeline walk is this one.
    cues: tuple[CueMember, ...] = ()

    def for_player(self, player_id: str) -> PlayerProposal | None:
        return next((player for player in self.players if player.player_id == player_id), None)


def eligible(candidate: Candidate, target: FitTarget, crop_limit: float,
             settings: FitSettings) -> bool:
    """Hard original-media policy, also applied to every authored alternative: the photo fit
    rule's size-only part (central/wall_plan/assignment.py `candidate_shape_fit` is None) on the
    Frame `target` at `crop_limit` with `settings`, at the candidate's readable size (by shape
    alone while it is not measured). Key content is judged by the wall assignment once
    measured."""
    return candidate_shape_fit(candidate, target, crop_limit, settings) is None


# What Central may revise on a layer it has already offered (each offered layer is a lock:
# its content and interval are fixed): the arbitration; the fade-out, which a Finish or a
# follower revises (`runtime._may_hold_to_the_end`); and the after-state, which is not content
# (a Run's after-states are fixed by its Scene; migration 071 left in-flight endings'
# offered layers at "leave_as_is" while their Scene says "keep_nothing"); and whether it awaits a
# grant (decision 0032: a lock offered before that release has none, and reads False); and Central's
# cut (decision 0036: Back to normal, a takeover's end, Carry on), which never touches the layer's
# content or identity, with the fade-out it ends.
REVISABLE = frozenset({"priority", "root_order", "admission_order", "fade_out", "after_end",
                       "awaits_grant", "cut_at"})
# Diagnostics that are no planning issue (central/planning_outcome.py `issues_of`): a lock of a Run
# Central cut (decision 0036), withdrawn because its turn starts at or after the cut.
NOT_ISSUES: Final = frozenset({"lock_cut"})


def same_execution(first: Layer, second: Layer) -> bool:
    """The same execution of an assignment, up to what Central may revise (`REVISABLE`)."""
    return first.model_dump(exclude=REVISABLE) == second.model_dump(exclude=REVISABLE)


def assignment_id(intent: Intent) -> str:
    identity = json.dumps(
        [intent.run_id, intent.target, intent.phase, intent.interval_start, intent.interval_end],
        separators=(",", ":"), allow_nan=False,
    )
    return "assignment-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanningError(f"{name} must be a finite number")
    return float(value)


def _drawn_end(intent: Intent) -> float:
    """Where an Intent's layer is drawn to: its turn's end, or its Run's cut (decision 0036)."""
    return intent.interval_end if intent.cut_at is None else min(intent.interval_end, intent.cut_at)


def _in_window(intent: Intent, window: PlanWindow) -> bool:
    """An Intent the plan horizon holds: on a Frame, showing before the plan's end, not over."""
    return (intent.kind != "actuator" and intent.shows_from < window.plan_end
            and _drawn_end(intent) > window.now)


def _cut_instant(starts: Sequence[float], budget: int) -> float | None:
    """The one instant a plan holding more than `budget` showings is cut at: the start of the
    first showing past the budget, in start order (every showing starting at or after it is
    dropped, so each Frame's plan ends at the same instant). None within the budget."""
    if len(starts) <= budget:
        return None
    return sorted(starts)[budget]


Standing = Literal["usable", "preparing", "failed_to_prepare", "no_compatible_variant"]


def candidate_standing(candidate: Candidate, target: FitTarget, settings: FitSettings) -> Standing:
    """What planning does with one candidate eligible for a Frame, as to its prepared copy: a
    recorded preparation failure drops it; with no copy yet, a slot inside the preparation
    horizon requests one and a later slot may take it tentatively; a copy the Frame cannot draw
    (central/wall_plan/assignment.py `variant_usable`) is never taken. The operator candidates
    route serves it as is."""
    if candidate.preparation_failure:
        return "failed_to_prepare"
    if candidate.variant is None:
        return "preparing"
    return "usable" if variant_usable(candidate, target, settings) else "no_compatible_variant"


def _after_end(intent: Intent, *, still: bool) -> AfterEnd:
    """The layer's after-state: the authored one, except that only a still is kept
    (central/wall_plan/assignment.py `layer_after_end`)."""
    return layer_after_end(intent.after_end, "image" if still else "video")


# The diagnostic each unfilled slot inside the preparation horizon raises.
_UNFILLED_CODES: dict[UnfilledReason, str] = {
    "no_photo_fits": "no_eligible_candidates",
    "no_photo_free": "no_photo_free",
    "authored_no_eligible_alternative": "authored_no_eligible_alternative",
}


@dataclass(frozen=True, slots=True)
class _Pending:
    """A media slot waiting for the wall assignment, with what its layer needs."""

    intent: Intent
    player_id: str
    fields: dict
    slot: Slot


class _ProjectionBuilder:
    def __init__(
        self,
        *,
        bindings_by_player: Mapping[str, Sequence[OutputBinding]],
        sources: Mapping[str, SourceLine],
        lines: SceneLines,
        authored: Mapping[str, Candidate],
        locks: Mapping[str, Layer],
        limits: PlannerLimits,
        settings: FitSettings,
        frame_outputs: Mapping[str, FrameOutput],
        house: HouseTime,
        never: frozenset[str] = frozenset(),
    ) -> None:
        self.house = house          # On this day's date (roadmap 2b)
        # Decision 0036: the Never show list's photos, and the `joins` covers' selections (their
        # pass is known once the lines are read).
        self.never = never
        self.joined: list[tuple[Layer, str, Layer | None]] = []
        self.limits, self.sources, self.lines, self.authored, self.locks = (
            limits, sources, lines, authored, locks)
        self.failure_checks: list[tuple[Intent, str, str]] = []
        self.read_by_key: dict[str, dict[str, Candidate]] = {}
        self.settings, self.frame_outputs = settings, frame_outputs
        self.frames: dict[str, tuple[str, OutputBinding]] = {}
        self.layers: dict[str, list[Layer]] = {player: [] for player in bindings_by_player}
        self.diagnostics: dict[tuple, Diagnostic] = {}
        self.diagnostics_dropped = 0
        self.selections: list[AssignmentSelection] = []
        self.seen: set[str] = set()
        self.used_locks: dict[str, Layer] = {}
        # Decision 0036: the Runs Central cut (Back to normal, a takeover's end), by id.
        self.cut_runs: set[str] = set()
        self.fixed: list[FixedShowing] = []
        self.pending: list[_Pending] = []
        self.pool_refs: dict[str, tuple[str, ...]] = {}
        self.cut: float | None = None
        self.cues: dict[str, CueMember] = {}
        self.wanted: dict[str, tuple[str, UnfilledSpan]] = {}   # media Intents inside the horizon
        # Decision 0032, set by `plan`: the walk's committed extents and each offered media
        # layer's photo, for the continuation lists.
        self.on_screen: tuple = ()
        self.photo_of: dict[str, str] = {}
        self._validate_inputs(bindings_by_player)

    def _validate_inputs(self, bindings: Mapping[str, Sequence[OutputBinding]]) -> None:
        for source, line in self.sources.items():
            if line.source_ref != source:
                raise PlanningError("source pool key does not match its immutable source reference")
        for key, layer in self.locks.items():
            if key != layer.assignment_id:
                raise PlanningError("locked assignment key mismatch")
        for player, outputs in bindings.items():
            local_outputs = set()
            for binding in outputs:
                target = f"frame:{binding.frame_id}"
                if target in self.frames:
                    raise PlanningError("a Frame may have only one current binding")
                if binding.output_id in local_outputs:
                    raise PlanningError("duplicate Output within a Player")
                local_outputs.add(binding.output_id)
                self.frames[target] = (player, binding)

    def diagnose(
        self, code: str, *, intent: Intent | None = None,
        assignment: str | None = None, source: str | None = None, reason: str | None = None,
    ) -> None:
        """Record one diagnostic, once per (code, Intent, assignment, Source, reason); past
        `max_diagnostics` it is counted, not kept."""
        key = (code, intent.run_id if intent else None, intent.target if intent else None,
               assignment, source, reason)
        if key in self.diagnostics:
            return
        if len(self.diagnostics) >= self.limits.max_diagnostics:
            self.diagnostics_dropped += 1
            return
        self.diagnostics[key] = Diagnostic(
            code=code, target=intent.target if intent else None, assignment_id=assignment,
            source_ref=source, run_id=intent.run_id if intent else None,
            scene_id=intent.scene_id if intent else None, reason=reason,
        )

    def cut_at(self, instant: float, reason: str) -> None:
        """The one cut rule: a budget shortens the plan at `instant` (`plan_shortened`, `reason`
        the budget). Every showing starting to show at or after the earliest cut is left out,
        except a lock (already offered content, never withdrawn by a budget), and the plan is
        valid no further than the cut (`result`)."""
        self.cut = instant if self.cut is None else min(self.cut, instant)
        self.diagnose("plan_shortened", reason=reason)

    def past_cut(self, start: float, identity: str) -> bool:
        """Whether a showing starting to show at `start` is left out by the cut (`cut_at`)."""
        return self.cut is not None and start >= self.cut and identity not in self.locks

    def refuse(self, intent: Intent, error: Exception, identity: str | None = None) -> None:
        """One showing (or one Scene's showings) the Planner could not plan: `intent_refused`
        naming its Scene and Run, and no cue waits on it."""
        if identity is not None:
            self.cues.pop(identity, None)
        self.diagnose("intent_refused", intent=intent, reason=_refusal(error))

    def _source_diagnostics(self, intent: Intent, identity: str) -> None:
        for source in intent.source_refs if not intent.asset_refs else ():
            line = self.sources.get(source)
            if line is None:
                self.diagnose("source_missing", intent=intent, assignment=identity, source=source)
            elif line.empty:
                self.diagnose("source_empty" if line.ready else "source_not_ready",
                              intent=intent, assignment=identity, source=source)
            else:   # judged over the photos this pass reads (`_failure_diagnostics`)
                self.failure_checks.append((intent, identity, source))

    @staticmethod
    def _layer_fields(intent: Intent, binding: OutputBinding, identity: str) -> dict:
        return dict(
            assignment_id=identity, run_id=intent.run_id,
            output_id=binding.output_id, frame_id=binding.frame_id,
            binding_generation=binding.generation,
            # The layer lasts while its photo can be seen: from when it starts to show (a
            # cross-fade's lead included, central/runtime.py `_transition_at`) to its cut; a
            # video starts as it appears.
            start=intent.shows_from, end=intent.interval_end,
            media_origin=intent.shows_from,
            priority=intent.priority, root_order=intent.root_order,
            admission_order=intent.admission_order,
            opacity=intent.base_opacity,
            fade_in=intent.fade_in_seconds, fade_out=intent.fade_out_seconds,
            # Decision 0032: a grant only for a change, the Run's first turn on the wall.
            awaits_grant=intent.cycle_index == 0,
            # Decision 0036: its Run's cut, when that falls inside the turn.
            cut_at=intent.cut_at,
        )

    def add(self, intent: Intent, window: PlanWindow) -> None:
        """One Intent of the plan horizon: a lock or a black layer is fixed (a layer when it
        starts inside the preparation horizon); a media Intent becomes a wall-plan slot. Each
        starts when its photo starts to show (`Intent.shows_from`). One placed inside the
        preparation horizon (or a lock) whose layer awaits a grant (its Run's first turn,
        decision 0032) is a cue member; one that raises adds nothing."""
        if not _in_window(intent, window):
            return
        identity = assignment_id(intent)
        if identity in self.seen:
            return
        self.seen.add(identity)
        self._want(intent, window, identity)
        self._place(intent, window, identity)
        if intent.cycle_index == 0 and (
                intent.shows_from < window.preparation_end or identity in self.locks):
            self.cues[identity] = CueMember(
                assignment_id=identity, root_id=intent.root_id, target=intent.target,
                shows_from=intent.shows_from, interval_end=intent.interval_end)

    def _want(self, intent: Intent, window: PlanWindow, identity: str) -> None:
        """Record an Intent inside the preparation horizon on a bound Frame, before anything can
        refuse it: `_unfilled` makes every one that ends the pass with no layer a span, so no
        refusal path can forget one. A black Intent always ends with its layer, so never one."""
        owner = self.frames.get(intent.target)
        if owner is not None and intent.shows_from < window.preparation_end:
            self.wanted[identity] = (owner[0], UnfilledSpan(
                output_id=owner[1].output_id, start=intent.shows_from, end=_drawn_end(intent)))

    def _unfilled(self, player: str, layers: Sequence[Layer]) -> tuple[UnfilledSpan, ...]:
        """`player`'s wanted showings that have no layer and are not past the cut, one span each."""
        placed = {layer.assignment_id for layer in layers}
        spans = [span for identity, (owner, span) in self.wanted.items()
                 if owner == player and identity not in placed
                 and not self.past_cut(span.start, identity)]
        return tuple(sorted(spans, key=lambda span: (span.output_id, span.start)))

    def _place(self, intent: Intent, window: PlanWindow, identity: str) -> None:
        inside = intent.shows_from < window.preparation_end
        owner = self.frames.get(intent.target)
        if owner is None:
            if inside:
                self.diagnose("frame_unbound", intent=intent, assignment=identity)
            return
        player, binding = owner
        fields = self._layer_fields(intent, binding, identity)
        if intent.kind == "media" and inside:
            self._source_diagnostics(intent, identity)
        locked = self.locks.get(identity)
        if locked is not None:
            expected_kind = "black" if intent.kind == "black" else "media"
            if any(
                getattr(locked, field) != value
                for field, value in fields.items() if field not in REVISABLE
            ) or locked.presentation != expected_kind:
                self.diagnose("lock_stale_authority", intent=intent, assignment=identity)
                return
            revised = {**fields, "after_end": _after_end(
                intent, still=locked.variant is not None and locked.variant.media_type != "video/mp4")}
            layer = Layer.model_validate({
                **locked.model_dump(), **{field: revised[field] for field in REVISABLE},
            })
            self.used_locks[identity] = layer
            self.layers[player].append(layer)
            self.selections.append(AssignmentSelection(assignment_id=identity, locked=True))
            return
        if intent.kind == "black":
            after_end = _after_end(intent, still=False)
            showing = FixedShowing(
                slot_id=identity, frame_id=binding.frame_id, asset_id=None,
                start=intent.shows_from, end=_drawn_end(intent),
                fade_in=intent.fade_in_seconds, after_end=after_end)
            layer = Layer(**fields, presentation="black", after_end=after_end) if inside else None
            self.fixed.append(showing)
            if layer is not None:
                self.layers[player].append(layer)
            return
        authored = bool(intent.asset_refs)
        pool = "" if authored else json.dumps(list(intent.source_refs), separators=(",", ":"))
        pending = _Pending(intent=intent, player_id=player, fields=fields, slot=Slot(
            slot_id=identity, frame_id=binding.frame_id, scene_id=intent.scene_id,
            start=intent.shows_from, end=_drawn_end(intent),
            fade_in=intent.fade_in_seconds, after_end=intent.after_end,
            target=self._target(binding.frame_id),
            crop_limit=crop_limit_of(intent.selection, self.settings),
            pool=pool, authored=intent.asset_refs))
        if not authored:
            self.pool_refs[pool] = intent.source_refs
        self.pending.append(pending)

    def add_covers(self, covers: Sequence[Cover], intents: Sequence[Intent],
                   window: PlanWindow) -> None:
        """Decision 0036: each live cover of an everyday action, above its Frame's layers. An
        offered one is a lock, re-emitted unchanged; a `joins` one not offered yet is emitted as
        built; a `pick` or `pinned` one is a wall-plan slot walked first (`Slot.action`). A cover
        whose Frame is bound elsewhere now, whose Run has no Intent on its Frame any more, or
        whose span is over is dropped. A wall's covers are one cue (`Cover.cue`): they await a
        grant and start whole before their instant wherever every Pi is ready. A cover of a Run
        Central cut (decision 0036) is cut with it, and one starting at or after the cut is not
        offered."""
        cuts: dict[str, tuple[float, float]] = {}
        for intent in intents:
            if intent.cut_at is not None and intent.run_id not in cuts:
                cuts[intent.run_id] = (intent.cut_at, intent.fade_out_seconds)
        for cover in covers:
            owner = self.frames.get(f"frame:{cover.frame_id}")
            if owner is None:
                continue
            player, binding = owner
            if (binding.output_id, binding.generation) != (cover.output_id, cover.binding_generation):
                continue
            cut = cuts.get(cover.run_id)
            locked = self.locks.get(cover.cover_id)
            if locked is not None:
                locked = _cut_layer(locked, cut)
                if locked is not None and locked.drawn_end > window.now:
                    self.used_locks[cover.cover_id] = locked
                    self.layers[player].append(locked)
                    self.selections.append(AssignmentSelection(assignment_id=cover.cover_id,
                                                               locked=True))
                    self._cue(cover, locked.start)
                continue
            start = cover_start(cover, window.now)
            intent = _run_intent(intents, cover, start)
            if intent is None or not still_due(cover, start):
                continue
            if cut is not None and start >= cut[0]:
                continue
            if cover.how == "joins":
                layer = _cut_layer(joined_layer(cover, start), cut)
                self.layers[player].append(layer)
                self.fixed.append(FixedShowing(
                    slot_id=layer.assignment_id, frame_id=cover.frame_id,
                    asset_id=layer.variant.asset_id if layer.variant else None, start=layer.start,
                    end=layer.drawn_end, fade_in=layer.fade_in, after_end=layer.after_end))
                self.joined.append((layer, intent.scene_id, None))
                self._cue(cover, start)
                continue
            authored = (cover.pinned,) if cover.how == "pinned" and cover.pinned else ()
            pool = "" if authored else json.dumps(list(intent.source_refs), separators=(",", ":"))
            if pool:
                self.pool_refs[pool] = intent.source_refs
            fields = layer_fields(cover, cover.frame_id, start)
            if cut is not None and cut[0] < cover.end:
                fields.update(cut_at=cut[0], fade_out=min(cut[1], cut[0] - start))
            self.pending.append(_Pending(
                intent=intent, player_id=player, fields=fields,
                slot=Slot(slot_id=cover.cover_id, frame_id=cover.frame_id, scene_id=intent.scene_id,
                          start=start, end=min(cover.end, fields.get("cut_at") or cover.end),
                          fade_in=cover.fade_in,
                          after_end=cover.after_end, target=self._target(cover.frame_id),
                          crop_limit=crop_limit_of(intent.selection, self.settings), pool=pool,
                          authored=authored, action=True, excluded=cover.left, repeat=False)))
            self._cue(cover, start)

    def add_holds(self, holds: Sequence[Pause], window: PlanWindow) -> None:
        """Decision 0036: each live pause's hold, above every Run (a Show now admitted during a
        pause plays beneath it, the owner's S6), emitted as the command built it: an offered one
        is a lock re-emitted with Carry on's cut (a revisable field); one not offered yet starts no
        sooner than `SKIP_LEAD` after this pass. Its extent counts against every placement, so no
        new placement gives its photo to another Frame meanwhile; one already offered elsewhere
        may overlap it (the owner's S4). A hold whose Frame is bound elsewhere now, or whose span
        is over, is dropped."""
        for pause in holds:
            owner = self.frames.get(f"frame:{pause.frame_id}")
            if owner is None:
                continue
            player, binding = owner
            held = pause.held
            if (binding.output_id, binding.generation) != (held.output_id, held.binding_generation):
                continue
            locked = self.locks.get(held.assignment_id)
            if locked is not None:
                layer = locked
                if held.cut_at is not None and held.cut_at < locked.drawn_end and held.cut_at > locked.start:
                    layer = Layer.model_validate({**locked.model_dump(), "cut_at": held.cut_at,
                                                  "fade_out": min(held.fade_out, held.cut_at - locked.start)})
            else:
                start = max(held.start, window.now + SKIP_LEAD)
                if start >= held.drawn_end:
                    continue
                layer = held if start == held.start else Layer.model_validate({
                    **held.model_dump(), "start": start,
                    "fade_out": min(held.fade_out, held.drawn_end - start)})
            if layer.drawn_end <= window.now:
                continue
            self.layers[player].append(layer)
            self.fixed.append(FixedShowing(
                slot_id=layer.assignment_id, frame_id=pause.frame_id,
                asset_id=layer.variant.asset_id if layer.variant else None, start=layer.start,
                end=layer.drawn_end, fade_in=layer.fade_in, after_end=layer.after_end))
            if locked is not None:
                self.used_locks[layer.assignment_id] = layer
                self.selections.append(AssignmentSelection(assignment_id=layer.assignment_id, locked=True))
            else:
                self.joined.append((layer, pause.scene_id, None))

    def _cue(self, cover: Cover, start: float) -> None:
        if cover.cue is not None:
            self.cues[cover.cover_id] = CueMember(
                assignment_id=cover.cover_id, root_id=cover.cue, target=f"frame:{cover.frame_id}",
                shows_from=start, interval_end=cover.end)

    def _target(self, frame_id: str) -> FitTarget | None:
        """The slot target of a Frame: `fit_target` of its Frame output; None when it has none
        (or is absent from `frame_outputs`), so its slot takes no photo."""
        output = self.frame_outputs.get(frame_id)
        return None if output is None else fit_target(output)

    def _pool_keys(self, pool: str) -> frozenset[str]:
        """The photo library pool keys a slot pool's Sources name (a Source with none adds none)."""
        return frozenset(line.pool_key for ref in self.pool_refs.get(pool, ())
                         if (line := self.sources.get(ref)) is not None
                         and line.pool_key is not None)

    def _read_lines(self, history: WallHistory, now: float) -> tuple[
            dict[str, Candidate], dict[str, frozenset[str]], dict[str, SceneLine]]:
        """Follow-up S3 (design rule 3: the Planner reads only what the plan needs, fresh, every
        pass). Each Scene with Scene-line slots reads the top of its line (`SceneLines.line`):
        its slots x `limits.hydrate_factor` members, the batch doubled while it holds fewer
        useful members (`useful_for`) than the line has slots, up to the whole line; and, when this pass could
        end in this plan (every member it has not picked is read, and no more of them are
        pickable than it has slots), the top of the next pass too. Returns every candidate a slot
        may take (the members read, then the authored ones), each slot pool's members read, and
        one line per Scene. Pickable: the members read that could fill one of its slots (the
        hand-placed ones excepted, which no line takes)."""
        reserved = {ref for item in self.pending for ref in item.slot.authored if not item.slot.action}
        passes = scene_passes(history)
        shown_at = last_shown_at(history)
        today = house_date(self.house, now)
        by_scene: dict[str, list[_Pending]] = {}
        for item in self.pending:
            if item.slot.pool:
                by_scene.setdefault(item.slot.scene_id, []).append(item)
        candidates: dict[str, Candidate] = {}
        members: dict[str, set[str]] = {pool: set() for pool in self.pool_refs}
        lines: dict[str, SceneLine] = {}
        refused: set[str] = set()
        for scene_id, items in sorted(by_scene.items()):
            # The Scene is the unit of isolation: one whose line cannot be read leaves its own
            # slots out (`intent_refused`), never another Scene's.
            try:
                known, keys_of, line = self._read_line(
                    scene_id, items, reserved, passes, shown_at, now, today)
            except Exception as error:   # noqa: BLE001 - the bulkhead names it per Scene
                refused.add(scene_id)
                for item in items:
                    self.refuse(item.intent, error, item.slot.slot_id)
                continue
            for asset_id, member in known.items():
                candidates.setdefault(asset_id, member.candidate)
                for pool, keys in keys_of.items():
                    if member.pools & keys:
                        members[pool].add(asset_id)
                for key in member.pools:
                    self.read_by_key.setdefault(key, {})[asset_id] = member.candidate
            lines[scene_id] = line
        self.pending = [item for item in self.pending
                        if not (item.slot.pool and item.slot.scene_id in refused)]
        for item in self.pending:
            for ref in item.slot.authored:
                if ref in self.authored:
                    candidates.setdefault(ref, self.authored[ref])
        return candidates, {pool: frozenset(ids) for pool, ids in members.items()}, lines

    def _read_line(self, scene_id: str, items: list[_Pending], reserved: set[str],
                   passes: Mapping[str, ScenePass], shown_at, now: float, today,
                   ) -> tuple[dict[str, LineMember], dict[str, frozenset[str]], SceneLine]:
        """One Scene's read of its line (`_read_lines`): the members it read, each slot pool's
        keys, and its `SceneLine`. Nothing of the builder changes here, so a Scene whose read
        raises leaves no trace but its refusal."""
        selection = items[0].intent.selection
        classes: dict[tuple, Slot] = {}
        for item in items:
            classes.setdefault((item.slot.pool, item.slot.target, item.slot.crop_limit),
                               item.slot)
        keys_of = {slot.pool: self._pool_keys(slot.pool) for slot in classes.values()}
        pool_keys = tuple(sorted(set().union(*keys_of.values())))
        scene_pass = passes.get(scene_id, ScenePass(scene_id=scene_id))
        recent = recent_assets(shown_at, selection.no_repeat_days, now=now)
        shape = shape_bounds(tuple(classes.values()))

        def read(pass_number: int, picked: frozenset[str], limit: int) -> LineAnswer:
            if not pool_keys:
                return LineAnswer()
            return self.lines.line(LineQuery(
                pool_keys=pool_keys, scene_id=scene_id, pass_number=pass_number,
                order=selection.order, after=scene_pass.last_shown, now=now, picked=picked, recent=recent,
                shape=shape, limit=limit, house=self.house))

        def serves(member: LineMember, test) -> bool:
            return member.candidate.asset_id not in reserved and any(
                member.pools & keys_of[slot.pool] and test(member.candidate, slot,
                                                           self.settings)
                for slot in classes.values())

        limit = len(items) * self.limits.hydrate_factor
        answer = read(scene_pass.pass_number, scene_pass.picked, limit)
        # Read deeper while the batch holds fewer useful photos than the line has slots
        # (and the line goes on): a sparse batch would otherwise fill every slot by
        # repeating the few it found, where a whole-pool read rotates through the fitting
        # photos further down the line.
        while (len(answer.members) == limit
               and sum(serves(member, useful_for) for member in answer.members) < len(items)):
            limit *= 2
            answer = read(scene_pass.pass_number, scene_pass.picked, limit)
        whole = len(answer.members) < limit
        complete = whole or answer.members[-1].candidate.asset_id in scene_pass.picked
        known = {member.candidate.asset_id: member for member in answer.members}
        pickable = {asset_id for asset_id, member in known.items()
                    if serves(member, fits_slot)}
        if not whole and complete and len(pickable - scene_pass.picked) <= len(items):
            for member in read(scene_pass.pass_number + 1, frozenset(), limit).members:
                if member.candidate.asset_id not in known:
                    known[member.candidate.asset_id] = member
                    if serves(member, fits_slot):
                        pickable.add(member.candidate.asset_id)
        line = SceneLine(
            scene_id, selection, tuple(member.candidate for member in known.values()),
            scene_pass, shown_at, now=now, today=today, pickable=frozenset(pickable),
            whole=whole, complete=complete, mark=answer.after, house=self.house)
        return known, keys_of, line

    def _failure_diagnostics(self) -> None:
        """`asset_preparation_failed` for a Source whose photos read this pass include one
        whose preparation failed."""
        for intent, identity, source in self.failure_checks:
            line = self.sources[source]
            read = self.read_by_key.get(line.pool_key or "", {})
            if any(candidate.preparation_failure for candidate in read.values()):
                self.diagnose("asset_preparation_failed", intent=intent, assignment=identity,
                              source=source)

    def _fit_slot_budget(self, budget: int) -> None:
        """Content first (owner, 2026-10-11: "always ensuring that frames have content"): a
        wall whose hour of slots exceeds the wall plan's slot budget is planned over a shorter
        span, never refused. The slots are cut at the start of the first slot past the budget,
        inside the preparation horizon or after it, so every Frame's plan ends at one instant,
        and the diagnostic `plan_shortened` (reason "slots") says so (`cut_at`: the black
        showings past the cut go too, a lock stays)."""
        if len(self.pending) <= budget:
            return
        self.cut_at(_cut_instant([item.slot.start for item in self.pending], budget), "slots")
        self.pending = [item for item in self.pending
                        if not self.past_cut(item.slot.start, item.slot.slot_id)]
        self.fixed = [showing for showing in self.fixed
                      if not self.past_cut(showing.start, showing.slot_id)]
        self.layers = {player: [layer for layer in layers
                                if not self.past_cut(layer.start, layer.assignment_id)]
                       for player, layers in self.layers.items()}
        self.cues = {identity: cue for identity, cue in self.cues.items()
                     if not self.past_cut(cue.shows_from, identity)}

    def _lock_photos(self, history: WallHistory) -> dict[str, str]:
        """Which photo each lock shows: its selection in the history, else (a lock offered
        before 5a, whose selection was never recorded) the photo revision its prepared copy is
        named by (`Variant.asset_id`, follow-up T), so a lock's photo is known whether or not
        this pass read it (follow-up S3)."""
        selected = {showing.assignment_id: showing.asset_id for showing in history.selected}
        for identity, lock in self.locks.items():
            layer = self.used_locks.get(identity, lock)
            if identity not in selected and layer.variant is not None:
                selected[identity] = layer.variant.asset_id
        return selected

    def plan(self, window: PlanWindow, history: WallHistory, max_clock_uncertainty: float,
             reserved: Sequence[Reservation] = (),
             frame_uncertainty: Mapping[str, float] | None = None,
             ) -> tuple[list[PlannedPhoto], list[AcquisitionRequest], list[MeasureRequest]]:
        """The wall assignment over the window; its slots inside the preparation horizon become
        layers and selections."""
        wall_limits = WallLimits(max_measurements=self.limits.max_measurements,
                                 max_preparations=self.limits.max_acquisitions)
        self._fit_slot_budget(wall_limits.max_slots)
        candidates, pools, lines = self._read_lines(history, window.now)
        self._failure_diagnostics()
        selected = self._lock_photos(history)
        for identity, lock in self.locks.items():
            layer = self.used_locks.get(identity, lock)
            self.fixed.append(FixedShowing(
                slot_id=identity, frame_id=layer.frame_id,
                asset_id=selected.get(identity) if layer.presentation == "media" else None,
                start=layer.start, end=layer.drawn_end, fade_in=layer.fade_in,
                after_end=layer.after_end))
        current_pass = {scene_id: line.pass_number for scene_id, line in lines.items()}
        selected_rows = {showing.assignment_id: showing for showing in history.selected}
        for layer, scene_id, _ in self.joined:
            row = selected_rows.get(layer.assignment_id)
            self.selections.append(AssignmentSelection(
                assignment_id=layer.assignment_id, asset_id=layer.variant.asset_id if layer.variant else None,
                scene_id=scene_id, pass_number=row.pass_number if row is not None
                else current_pass.get(scene_id, 1)))
        wall = plan_wall(tuple(item.slot for item in self.pending), tuple(self.fixed),
                         history.kept, candidates, lines, pools, window=window,
                         max_clock_uncertainty=max_clock_uncertainty, limits=wall_limits,
                         settings=self.settings, reserved=reserved,
                         frame_uncertainty=frame_uncertainty, never=self.never)
        self.on_screen = wall.on_screen
        self.photo_of = {identity: asset_id for identity, asset_id in selected.items()
                         if identity in self.used_locks}
        placed = {placement.slot_id: placement for placement in wall.placed}
        unfilled = {gap.slot_id: gap.reason for gap in wall.unfilled}
        planned: list[PlannedPhoto] = []
        for item in self.pending:
            # Each slot is its own unit: one whose layer cannot be built is refused alone.
            try:
                photo, layer, selection = self._settle_slot(
                    item, window, placed, unfilled, candidates, current_pass)
            except Exception as error:   # noqa: BLE001 - the bulkhead names it per slot
                self.refuse(item.intent, error, item.slot.slot_id)
                continue
            planned.append(photo)
            if layer is not None:
                self.layers[item.player_id].append(layer)
                self.selections.append(selection)
                self.photo_of[layer.assignment_id] = photo.asset_id
        for identity, layer in self.used_locks.items():
            if layer.presentation == "media":
                planned.append(PlannedPhoto(
                    assignment_id=identity, frame_id=layer.frame_id, start=layer.start,
                    end=layer.drawn_end, state="locked", asset_id=selected.get(identity),
                    crop=layer.crop))
        planned.sort(key=lambda photo: (photo.start, photo.frame_id, photo.assignment_id))
        acquisitions = [AcquisitionRequest(asset_id=need.asset_id,
                                           assignment_ids=tuple(sorted(need.slot_ids)),
                                           earliest_start=need.earliest_start)
                        for need in sorted(wall.prepare, key=lambda need: need.asset_id)]
        measurements = [MeasureRequest(asset_id=asset_id) for asset_id in wall.measure]
        return planned, acquisitions, measurements

    def _settle_slot(self, item: _Pending, window: PlanWindow, placed: Mapping, unfilled: Mapping,
                     candidates: Mapping[str, Candidate], current_pass: Mapping[str, int],
                     ) -> tuple[PlannedPhoto, Layer | None, AssignmentSelection | None]:
        """One slot after the wall assignment: its planned photo, and its layer and selection
        when it was placed inside the preparation horizon."""
        slot, intent = item.slot, item.intent
        inside = slot.start < window.preparation_end
        entry = dict(assignment_id=slot.slot_id, frame_id=slot.frame_id,
                     start=slot.start, end=slot.end)
        placement = placed.get(slot.slot_id)
        if placement is None:
            reason = unfilled[slot.slot_id]
            if inside and not slot.action:   # a cover waiting for its photo is no planning issue
                self.diagnose(_UNFILLED_CODES[reason], intent=intent, assignment=slot.slot_id)
            return PlannedPhoto(**entry, state="unfilled", reason=reason), None, None
        candidate = candidates[placement.asset_id]
        photo = PlannedPhoto(**entry, state="offered" if inside else "tentative",
                             asset_id=placement.asset_id, crop=placement.crop)
        if not inside:
            return photo, None, None
        layer = Layer(
            **item.fields, presentation="media", variant=candidate.variant,
            crop=placement.crop, after_end=_after_end(intent, still=candidate.kind == "image"))
        selection = AssignmentSelection(
            assignment_id=slot.slot_id, asset_id=placement.asset_id, scene_id=slot.scene_id,
            pass_number=placement.pass_number or current_pass.get(slot.scene_id, 1))
        return photo, layer, selection

    def _segments(self, intents: Sequence[Intent], known_until: float,
                  ) -> list[ContinuationSegment]:
        """Decision 0032, the segments' one home: for each bound Frame and each Run with an
        offered media layer on it, the Run's first turn on that Frame past every layer and
        unfilled span its Output holds that no Intent of a Run of greater precedence covers;
        its pace, lead and fades; and `until`, the first later instant the Run has no Intent on
        the Frame or a Run of greater precedence starts to show there (None when neither falls
        before `known_until`, the timeline's end). The Run's offered media layers on the Frame,
        in start order, are its candidates. On one Output, a segment starting inside an earlier
        one's span is dropped, and one whose lead would reach into it starts at its end."""
        by_target: dict[str, list[Intent]] = {}
        for intent in intents:
            by_target.setdefault(intent.target, []).append(intent)
        segments: list[ContinuationSegment] = []
        for target, (player, binding) in sorted(self.frames.items()):
            on_frame = sorted(by_target.get(target, ()), key=lambda i: i.interval_start)
            layers = [layer for layer in self.layers[player]
                      if layer.output_id == binding.output_id]
            spans = [span for span in self._unfilled(player, self.layers[player])
                     if span.output_id == binding.output_id]
            boundary = max([layer.end for layer in layers] + [span.end for span in spans],
                           default=-math.inf)
            found: list[ContinuationSegment] = []
            for run_id in sorted({layer.run_id for layer in layers
                                  if layer.presentation == "media" and not layer.action}):
                candidates = tuple(
                    ListedPhoto(assignment_id=layer.assignment_id,
                                asset_id=self.photo_of[layer.assignment_id],
                                kept=layer.after_end == "keep_this_photo")
                    for layer in sorted(layers, key=lambda layer: layer.start)
                    if layer.run_id == run_id and layer.presentation == "media"
                    and not layer.action and layer.assignment_id in self.photo_of
                    and self.photo_of[layer.assignment_id] not in self.never)
                segment = _segment(run_id, on_frame, boundary, known_until)
                if segment is None or not candidates:
                    continue
                first, until = segment
                lead = first.interval_start - first.shows_from
                seconds = first.interval_end - first.interval_start
                if lead > seconds:
                    continue
                found.append(ContinuationSegment(
                    player_id=player, frame_id=binding.frame_id, output_id=binding.output_id,
                    run_id=run_id, start=first.interval_start, seconds=seconds, lead=lead,
                    fade_in=first.fade_in_seconds, fade_out=first.fade_out_seconds,
                    until=until, candidates=candidates))
            segments.extend(_in_turn(found))
        return segments

    def _continuations(self, intents: Sequence[Intent], known_until: float,
                       max_clock_uncertainty: float,
                       frame_uncertainty: Mapping[str, float] | None,
                       ) -> dict[str, tuple[Continuation, ...]]:
        """Each Player's continuations (central/wall_plan/continuation.py `continuation_lists`),
        kept off the walk's extents and every other Frame's list."""
        extents: dict[str, list[FrameExtent]] = {}
        for entry in self.on_screen:
            extents.setdefault(entry.asset_id, []).append(
                FrameExtent(frame_id=entry.frame_id, extent=entry.extent))
        guard = {binding.frame_id: 2 * frame_clock_uncertainty(
            binding.frame_id, max_clock_uncertainty, frame_uncertainty)
            for _, binding in self.frames.values()}
        return continuation_lists(self._segments(intents, known_until), extents=extents,
                                  guard=guard)

    def result(self, window: PlanWindow, history: WallHistory,
               max_clock_uncertainty: float, *, intents: Sequence[Intent] = (),
               known_until: float = math.inf, reserved: Sequence[Reservation] = (),
               frame_uncertainty: Mapping[str, float] | None = None) -> Projection:
        now, horizon_end = window.now, window.preparation_end
        planned, acquisitions, measurements = self.plan(
            window, history, max_clock_uncertainty, reserved, frame_uncertainty)
        for identity, lock in self.locks.items():
            if identity not in self.used_locks:
                # Absence from this horizon alone is not permission to erase a
                # secured lock. Coordination owns cancellation/expiry and pins. A lock of a Run
                # Central cut (decision 0036) is withdrawn on purpose: no planning issue.
                self.diagnose(
                    "lock_expired" if lock.drawn_end <= now
                    else "lock_cut" if lock.run_id in self.cut_runs else "lock_outside_projection",
                    assignment=identity,
                )
        continuations = self._continuations(intents, known_until, max_clock_uncertainty,
                                            frame_uncertainty)
        players = tuple(PlayerProposal(
            player_id=player,
            layers=tuple(sorted(layers, key=lambda layer: (
                *stack_order(layer), layer.output_id, layer.assignment_id))),
            unfilled=self._unfilled(player, layers),
            continuations=continuations.get(player, ()),
        ) for player, layers in sorted(self.layers.items()))
        # A layer is offered whole, so the plan stays valid to the end of its longest layer; a
        # cut plan holds nothing new past its cut, so it is valid no further than the cut.
        reach = horizon_end if self.cut is None else min(horizon_end, self.cut)
        extent = max([reach, *(layer.end for p in players for layer in p.layers)])
        return Projection(
            now=now, horizon_end=horizon_end, valid_until=extent, players=players,
            acquisitions=tuple(acquisitions),
            diagnostics=tuple(self.diagnostics.values()),
            selections=tuple(self.selections),
            measurements=tuple(measurements),
            plan=tuple(planned),
            diagnostics_dropped=self.diagnostics_dropped,
            cut=self.cut,
            cues=tuple(sorted(self.cues.values(), key=lambda cue: cue.assignment_id)),
        )


def project(
    runtime: Runtime,
    now: float,
    *,
    bindings_by_player: Mapping[str, Sequence[OutputBinding]],
    lines: SceneLines,
    authored_candidates: Mapping[str, Candidate],
    locked_assignments: Mapping[str, Layer],
    history: WallHistory,
    horizon_seconds: float = 300,
    plan_seconds: float = PLAN_SECONDS,
    max_clock_uncertainty: float = 0.1,
    limits: PlannerLimits = PlannerLimits(),
    settings: FitSettings,
    frame_outputs: Mapping[str, FrameOutput],
    reserved: Sequence[Reservation] = (),
    frame_uncertainty: Mapping[str, float] | None = None,
    covers: Sequence[Cover] = (),
    never: frozenset[str] = frozenset(),
    holds: Sequence[Pause] = (),
) -> Projection:
    """Build complete-cycle proposals without changing any supplied state.

    Decision 0032 (slice B): `reserved` and `frame_uncertainty` pass to the wall plan
    (central/wall_plan/assignment.py `plan_wall`); each layer's `awaits_grant` is its Intent's
    `cycle_index == 0`, and only such layers are cue members; each Player's proposal carries its
    continuations (`PlayerProposal.continuations`).

    Roadmap 5a (slice W1): the wall plan covers `plan_seconds` (never less than
    `horizon_seconds`) from `now`, every Frame together; layers are proposed only for slots
    starting before now + `horizon_seconds` (the preparation horizon). `history` is the shown
    history and the locks' photos (central/infra/shown_history_store.py); `max_clock_uncertainty`
    sets the on-screen extents' guard (contracts/on_screen.py). Follow-ups B1: every slot is
    judged with `settings` (the photo fit settings) against `fit_target(frame_outputs[frame_id])`
    (frame box run fb-8: a Frame without a Frame output, or absent from it, has no shape, so its
    slots take no photo and keep the Frame's last still, else the fallback); a Scene without its
    own crop limit follows `settings.crop_limit`. Follow-up S3: `lines` answers the runtime's
    Source revisions and the top of each Scene's line (`_ProjectionBuilder._read_lines`); nothing
    reads a whole pool.

    A pass never refuses the wall for one Scene: each Intent's `add`, each Scene's line read
    (`_read_line`) and each placed slot's layer (`_settle_slot`) is its own unit, and one that
    raises is left out alone, with the diagnostic `intent_refused` naming its Scene and Run. A
    budget shortens the plan at one instant (`cut_at`; `plan_shortened`, its `reason` the
    budget: "runtime_events", "assignments" or "slots"), never cutting a lock.

    Decision 0036 (slice 3): `covers` are the everyday actions' live covers, added above their
    Frames' layers (`_ProjectionBuilder.add_covers`); none is ever an unfilled span or a
    continuation's candidate. `never` is the Never show list's photos: no slot takes one, and no
    continuation lists one. `holds` are the live pauses' holds, above every Run
    (`_ProjectionBuilder.add_holds`); none is a continuation's candidate. Only inconsistent
    caller input (`_validate_inputs`, a non-finite time) raises, and a Runtime that cannot settle
    even `now` within its budget (`RuntimeBudgetExceeded`: an empty plan would withdraw every
    offered layer)."""
    now = _finite(now, "now")
    horizon_seconds = _finite(horizon_seconds, "horizon_seconds")
    if horizon_seconds <= 0:
        raise PlanningError("horizon_seconds must be positive")
    plan_span = _finite(plan_seconds, "plan_seconds")
    if plan_span <= 0:
        raise PlanningError("plan_seconds must be positive")
    plan_span = max(plan_span, horizon_seconds)
    max_clock_uncertainty = _finite(max_clock_uncertainty, "max_clock_uncertainty")
    if max_clock_uncertainty < 0:
        raise PlanningError("max_clock_uncertainty must not be negative")
    window = PlanWindow(now=now, preparation_end=_finite(now + horizon_seconds, "horizon end"),
                        plan_end=_finite(now + plan_span, "plan end"))
    builder = _ProjectionBuilder(
        bindings_by_player=bindings_by_player,
        sources=lines.sources(runtime.planning_source_refs()), lines=lines,
        authored=authored_candidates, locks=locked_assignments, limits=limits,
        settings=settings, frame_outputs=frame_outputs, house=runtime.house, never=never,
    )
    # A turn starting up to the longest cross-fade lead past the plan's end may start to show
    # inside it. Past the transition budget the timeline stops at its last whole boundary: the
    # plan is cut there (`cut_at`), holding only what starts to show before it.
    end = window.plan_end + runtime.longest_lead()
    timeline = runtime.timeline(now, end, max_events=limits.max_events)
    if not timeline.views:
        # Not even now fits the budget: an empty plan would withdraw every Frame's offered
        # layers, so this pass fails and the offers stand.
        raise RuntimeBudgetExceeded("the Runtime cannot settle now within its transition budget")
    if timeline.until < end:
        builder.cut_at(timeline.until, "runtime_events")
    intents = [intent for view in timeline.views for intent in view.contributions]
    builder.cut_runs = {run.run_id for view in timeline.views for run in view.runs
                        if run.until is not None}
    # Layers inside the preparation horizon past `max_assignments`: cut at one instant.
    inside = {assignment_id(intent): intent.shows_from for intent in intents
              if _in_window(intent, window) and intent.shows_from < window.preparation_end}
    cutoff = _cut_instant(list(inside.values()), limits.max_assignments)
    if cutoff is not None:
        builder.cut_at(cutoff, "assignments")
    for intent in intents:
        if builder.past_cut(intent.shows_from, assignment_id(intent)):
            continue
        try:
            builder.add(intent, window)
        except Exception as error:   # noqa: BLE001 - the per-Intent bulkhead
            builder.refuse(intent, error, assignment_id(intent))
    # Each showing once (an Intent recurs in every view it spans), for the continuations.
    showings = list({assignment_id(intent): intent for intent in reversed(intents)
                     if intent.kind != "actuator"}.values())
    builder.add_covers(covers, showings, window)
    builder.add_holds(holds, window)
    known_until = timeline.until if builder.cut is None else min(timeline.until, builder.cut)
    return builder.result(window, history, max_clock_uncertainty, intents=showings,
                          known_until=known_until, reserved=reserved,
                          frame_uncertainty=frame_uncertainty)


def _cut_layer(layer: Layer, cut: tuple[float, float] | None) -> Layer | None:
    """`layer` (an action layer) under its Run's cut (decision 0036: the instant and the fade-out
    that ends there): cut when the cut falls inside it (never later than a cut it carries), None
    when it starts at or after it, itself otherwise."""
    if cut is None or cut[0] >= layer.drawn_end:
        return layer
    at, fade = cut
    if at <= layer.start:
        return None
    return Layer.model_validate({**layer.model_dump(), "cut_at": at,
                                 "fade_out": min(fade, at - layer.start)})


def _run_intent(intents: Sequence[Intent], cover: Cover, start: float) -> Intent | None:
    """The covered Run's Intent on the cover's Frame at `start` (its pool, Scene and selection
    are the cover's), else the Run's latest one there before it; None when the Run has none
    there."""
    target = f"frame:{cover.frame_id}"
    mine = sorted((intent for intent in intents if intent.run_id == cover.run_id
                   and intent.target == target and intent.kind == "media"),
                  key=lambda intent: intent.interval_start)
    at = [intent for intent in mine if intent.shows_from <= start < intent.interval_end]
    if at:
        return at[-1]
    before = [intent for intent in mine if intent.shows_from <= start]
    return before[-1] if before else (mine[0] if mine else None)


def _segment(run_id: str, on_frame: Sequence[Intent], boundary: float, known_until: float,
             ) -> tuple[Intent, float | None] | None:
    """`_segments` for one Run on one Frame: its first turn at or past `boundary` that no Intent
    of greater precedence covers, and its `until`; None when it has none. `on_frame` is the
    Frame's Intents by turn."""
    mine = [intent for intent in on_frame if intent.run_id == run_id]
    others = [intent for intent in on_frame if intent.run_id != run_id]
    first = next((intent for intent in mine if intent.interval_start >= boundary and not any(
        other.precedence > intent.precedence
        and other.interval_start <= intent.interval_start < other.interval_end
        for other in others)), None)
    if first is None:
        return None
    covered = first.interval_end
    for intent in mine:
        if intent.interval_start > first.interval_start:
            if intent.interval_start > covered:
                break
            covered = max(covered, intent.interval_end)
    ends = [covered] if covered < known_until else []
    ends += [other.shows_from for other in others
             if other.precedence > first.precedence and other.shows_from > first.interval_start]
    return first, min(ends) if ends else None


def _in_turn(segments: Sequence[ContinuationSegment]) -> list[ContinuationSegment]:
    """One Output's segments one after the other: a segment starting before an earlier one's
    `until` (or after one with none) is dropped; one whose lead reaches into the earlier one's
    span starts at its `until`, its fade-in cut to fit."""
    kept: list[ContinuationSegment] = []
    for segment in sorted(segments, key=lambda segment: segment.start):
        if kept:
            earlier = kept[-1].until
            if earlier is None or segment.start < earlier:
                continue
            if segment.start - segment.lead < earlier:
                lead = segment.start - earlier
                fade_out = min(segment.fade_out, segment.seconds + lead)
                segment = segment.model_copy(update={
                    "lead": lead, "fade_out": fade_out,
                    "fade_in": min(segment.fade_in, segment.seconds + lead - fade_out)})
        kept.append(segment)
    return kept


def _refusal(error: Exception) -> str:
    """Why one Intent was refused, in the operator's words, for its `intent_refused` diagnostic:
    a PlanningError's own message (the Planner writes it), else a plain sentence naming only the
    error's type (another error's text can carry the values it judged)."""
    if isinstance(error, PlanningError):
        return str(error)
    return f"Central could not build its showing ({type(error).__name__})"
