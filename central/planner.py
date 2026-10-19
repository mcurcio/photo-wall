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
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from central.catalog import Candidate
from central.house_time.calculator import house_date
from central.house_time.model import HouseTime
from central.installation_models import FrameOutput
from central.runtime import Intent, Runtime, RuntimeBudgetExceeded
from central.wall_plan.assignment import (
    FixedShowing,
    PlanWindow,
    Slot,
    UnfilledReason,
    WallLimits,
    candidate_shape_fit,
    fits_slot,
    layer_after_end,
    plan_wall,
    shape_bounds,
    useful_for,
    variant_usable,
)
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
    Identifier,
    Instant,
    Layer,
    Model,
    OutputBinding,
    PhotoCrop,
    stack_order,
)

if TYPE_CHECKING:
    from central.execution_outcomes import ExecutionOutcome


class PlanningError(ValueError):
    """Invalid configuration or inconsistent input prevents a complete proposal."""


class PlanningBudgetExceeded(PlanningError):
    """No partial plan is returned after an explicit planning limit is exceeded."""


def handle_execution_outcome(outcome: ExecutionOutcome) -> str:
    """Own the next-planning consequence without changing secured content."""
    return {
        "group_skipped": "preserve_selection",
        "observation": "record_observation",
        "offer_backpressure": "await_capacity",
        "offer_unavailable": "await_media",
        "readiness_lost": "replan",
    }[outcome.kind]


# Roadmap 5a: how far ahead the wall plan reaches, every Frame together (owner, 2026-10-11: "an
# algorithm that computes all frame+photo assignments well in advance"; the plan horizon's 1 h
# default confirmed on the r2 gate page). Coordination's `plan_seconds` defaults to it.
PLAN_SECONDS = 3600.0


class PlannerLimits(Model):
    max_horizon_seconds: float = Field(default=PLAN_SECONDS, gt=0)
    max_cycle_seconds: float = Field(default=300, gt=0, le=300)
    max_events: int = Field(default=10000, ge=1)
    max_assignments: int = Field(default=4096, ge=1)
    max_sources: int = Field(default=128, ge=1)
    max_authored_refs: int = Field(default=10000, ge=1)
    max_acquisitions: int = Field(default=256, ge=1)
    max_diagnostics: int = Field(default=8192, ge=1)
    max_players: int = Field(default=128, ge=1)
    max_bindings: int = Field(default=256, ge=1)
    # Roadmap 5a: measurement requests per projection (central/wall_plan/assignment.py).
    max_measurements: int = Field(default=64, ge=0)
    # Follow-up S3: a Scene's line is read `hydrate_factor` members per slot at a time.
    hydrate_factor: int = Field(default=4, ge=1, le=64)


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


class PlayerProposal(Model):
    player_id: str
    layers: tuple[Layer, ...]


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
# offered layers at "leave_as_is" while their Scene says "keep_nothing").
REVISABLE = frozenset({"priority", "root_order", "admission_order", "fade_out", "after_end"})


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


def _bound(size: int | float, limit: int | float, name: str) -> None:
    if size > limit:
        raise PlanningBudgetExceeded(f"{name} budget exceeded")


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
    ) -> None:
        self.house = house          # On this day's date (roadmap 2b)
        self.limits, self.sources, self.lines, self.authored, self.locks = (
            limits, sources, lines, authored, locks)
        self.failure_checks: list[tuple[Intent, str, str]] = []
        self.read_by_key: dict[str, dict[str, Candidate]] = {}
        self.settings, self.frame_outputs = settings, frame_outputs
        self.frames: dict[str, tuple[str, OutputBinding]] = {}
        self.layers: dict[str, list[Layer]] = {player: [] for player in bindings_by_player}
        self.diagnostics: dict[tuple, Diagnostic] = {}
        self.selections: list[AssignmentSelection] = []
        self.seen: set[str] = set()
        self.layered = 0
        self.used_locks: dict[str, Layer] = {}
        self.fixed: list[FixedShowing] = []
        self.pending: list[_Pending] = []
        self.pool_refs: dict[str, tuple[str, ...]] = {}
        self._validate_inputs(bindings_by_player)

    def _validate_inputs(self, bindings: Mapping[str, Sequence[OutputBinding]]) -> None:
        _bound(len(bindings), self.limits.max_players, "Player count")
        _bound(sum(len(items) for items in bindings.values()), self.limits.max_bindings, "binding count")
        _bound(len(self.sources), self.limits.max_sources, "source count")
        _bound(len(self.locks), self.limits.max_assignments, "locked assignment count")
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
        assignment: str | None = None, source: str | None = None,
    ) -> None:
        target = intent.target if intent else None
        key = (code, target, assignment, source)
        if key not in self.diagnostics:
            _bound(len(self.diagnostics) + 1, self.limits.max_diagnostics, "diagnostic count")
            self.diagnostics[key] = Diagnostic(
                code=code, target=target, assignment_id=assignment, source_ref=source
            )

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
        )

    def add(self, intent: Intent, window: PlanWindow) -> None:
        """One Intent of the plan horizon: a lock or a black layer is fixed (a layer when it
        starts inside the preparation horizon); a media Intent becomes a wall-plan slot. Each
        starts when its photo starts to show (`Intent.shows_from`)."""
        if (intent.kind == "actuator" or intent.shows_from >= window.plan_end
                or intent.interval_end <= window.now):
            return
        _bound(len(intent.source_refs), self.limits.max_sources, "authored source reference count")
        _bound(len(intent.asset_refs), self.limits.max_authored_refs, "authored asset reference count")
        identity = assignment_id(intent)
        if identity in self.seen:
            return
        inside = intent.shows_from < window.preparation_end
        if inside:
            _bound(self.layered + 1, self.limits.max_assignments, "assignment count")
            self.layered += 1
        self.seen.add(identity)
        if intent.interval_end - intent.interval_start > self.limits.max_cycle_seconds:
            raise PlanningBudgetExceeded("cycle length exceeds supported preparation extent")
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
            self.fixed.append(FixedShowing(
                slot_id=identity, frame_id=binding.frame_id, asset_id=None,
                start=intent.shows_from, end=intent.interval_end,
                fade_in=intent.fade_in_seconds, after_end=after_end))
            if inside:
                self.layers[player].append(Layer(**fields, presentation="black",
                                                 after_end=after_end))
            return
        authored = bool(intent.asset_refs)
        pool = "" if authored else json.dumps(list(intent.source_refs), separators=(",", ":"))
        if not authored:
            self.pool_refs[pool] = intent.source_refs
        self.pending.append(_Pending(intent=intent, player_id=player, fields=fields, slot=Slot(
            slot_id=identity, frame_id=binding.frame_id, scene_id=intent.scene_id,
            start=intent.shows_from, end=intent.interval_end,
            fade_in=intent.fade_in_seconds, after_end=intent.after_end,
            target=self._target(binding.frame_id),
            crop_limit=crop_limit_of(intent.selection, self.settings),
            pool=pool, authored=intent.asset_refs)))

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
        reserved = {ref for item in self.pending for ref in item.slot.authored}
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
        for scene_id, items in sorted(by_scene.items()):
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

            def read(pass_number: int, picked: frozenset[str], limit: int, *, scene_id=scene_id,
                     selection=selection, pool_keys=pool_keys, recent=recent, shape=shape,
                     last=scene_pass.last_shown) -> LineAnswer:
                if not pool_keys:
                    return LineAnswer()
                return self.lines.line(LineQuery(
                    pool_keys=pool_keys, scene_id=scene_id, pass_number=pass_number,
                    order=selection.order, after=last, now=now, picked=picked, recent=recent,
                    shape=shape, limit=limit, house=self.house))

            def serves(member: LineMember, test, *, classes=classes, keys_of=keys_of) -> bool:
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
            for asset_id, member in known.items():
                candidates.setdefault(asset_id, member.candidate)
                for pool, keys in keys_of.items():
                    if member.pools & keys:
                        members[pool].add(asset_id)
                for key in member.pools:
                    self.read_by_key.setdefault(key, {})[asset_id] = member.candidate
            lines[scene_id] = SceneLine(
                scene_id, selection, tuple(member.candidate for member in known.values()),
                scene_pass, shown_at, now=now, today=today, pickable=frozenset(pickable),
                whole=whole, complete=complete, mark=answer.after, house=self.house)
        for item in self.pending:
            for ref in item.slot.authored:
                if ref in self.authored:
                    candidates.setdefault(ref, self.authored[ref])
        return candidates, {pool: frozenset(ids) for pool, ids in members.items()}, lines

    def _failure_diagnostics(self) -> None:
        """`asset_preparation_failed` for a Source whose photos read this pass include one
        whose preparation failed."""
        for intent, identity, source in self.failure_checks:
            line = self.sources[source]
            read = self.read_by_key.get(line.pool_key or "", {})
            if any(candidate.preparation_failure for candidate in read.values()):
                self.diagnose("asset_preparation_failed", intent=intent, assignment=identity,
                              source=source)

    def _fit_slot_budget(self, window: PlanWindow, budget: int) -> None:
        """Content first (owner, 2026-10-11: "always ensuring that frames have content"): a
        wall whose hour of slots exceeds the wall plan's slot budget is planned over a shorter
        span, never refused. The slots inside the preparation horizon are always kept (more of
        them than the budget is PlanningBudgetExceeded, as before 5a); the tentative tail is cut
        at the start of the first slot past the budget, so every Frame's plan ends at one instant,
        and the diagnostic `plan_shortened` says so."""
        if len(self.pending) <= budget:
            return
        inside = sum(item.slot.start < window.preparation_end for item in self.pending)
        _bound(inside, budget, "plan slot count")
        ordered = sorted(self.pending, key=lambda item: (item.slot.start, item.slot.slot_id))
        cutoff = ordered[budget].slot.start
        self.pending = [item for item in self.pending if item.slot.start < cutoff]
        self.diagnose("plan_shortened")

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
             ) -> tuple[list[PlannedPhoto], list[AcquisitionRequest], list[MeasureRequest]]:
        """The wall assignment over the window; its slots inside the preparation horizon become
        layers and selections."""
        wall_limits = WallLimits(max_measurements=self.limits.max_measurements,
                                 max_preparations=self.limits.max_acquisitions)
        self._fit_slot_budget(window, wall_limits.max_slots)
        candidates, pools, lines = self._read_lines(history, window.now)
        self._failure_diagnostics()
        selected = self._lock_photos(history)
        for identity, lock in self.locks.items():
            layer = self.used_locks.get(identity, lock)
            self.fixed.append(FixedShowing(
                slot_id=identity, frame_id=layer.frame_id,
                asset_id=selected.get(identity) if layer.presentation == "media" else None,
                start=layer.start, end=layer.end, fade_in=layer.fade_in,
                after_end=layer.after_end))
        current_pass = {scene_id: line.pass_number for scene_id, line in lines.items()}
        wall = plan_wall(tuple(item.slot for item in self.pending), tuple(self.fixed),
                         history.kept, candidates, lines, pools, window=window,
                         max_clock_uncertainty=max_clock_uncertainty, limits=wall_limits,
                         settings=self.settings)
        placed = {placement.slot_id: placement for placement in wall.placed}
        unfilled = {gap.slot_id: gap.reason for gap in wall.unfilled}
        planned: list[PlannedPhoto] = []
        for item in self.pending:
            slot, intent = item.slot, item.intent
            inside = slot.start < window.preparation_end
            entry = dict(assignment_id=slot.slot_id, frame_id=slot.frame_id,
                         start=slot.start, end=slot.end)
            placement = placed.get(slot.slot_id)
            if placement is None:
                reason = unfilled[slot.slot_id]
                if inside:
                    self.diagnose(_UNFILLED_CODES[reason], intent=intent, assignment=slot.slot_id)
                planned.append(PlannedPhoto(**entry, state="unfilled", reason=reason))
                continue
            candidate = candidates[placement.asset_id]
            planned.append(PlannedPhoto(
                **entry, state="offered" if inside else "tentative",
                asset_id=placement.asset_id, crop=placement.crop))
            if not inside:
                continue
            self.layers[item.player_id].append(Layer(
                **item.fields, presentation="media", variant=candidate.variant,
                crop=placement.crop, after_end=_after_end(intent, still=candidate.kind == "image")))
            self.selections.append(AssignmentSelection(
                assignment_id=slot.slot_id, asset_id=placement.asset_id, scene_id=slot.scene_id,
                pass_number=placement.pass_number or current_pass.get(slot.scene_id, 1)))
        for identity, layer in self.used_locks.items():
            if layer.presentation == "media":
                planned.append(PlannedPhoto(
                    assignment_id=identity, frame_id=layer.frame_id, start=layer.start,
                    end=layer.end, state="locked", asset_id=selected.get(identity),
                    crop=layer.crop))
        planned.sort(key=lambda photo: (photo.start, photo.frame_id, photo.assignment_id))
        acquisitions = [AcquisitionRequest(asset_id=need.asset_id,
                                           assignment_ids=tuple(sorted(need.slot_ids)),
                                           earliest_start=need.earliest_start)
                        for need in sorted(wall.prepare, key=lambda need: need.asset_id)]
        measurements = [MeasureRequest(asset_id=asset_id) for asset_id in wall.measure]
        return planned, acquisitions, measurements

    def result(self, window: PlanWindow, history: WallHistory,
               max_clock_uncertainty: float) -> Projection:
        now, horizon_end = window.now, window.preparation_end
        planned, acquisitions, measurements = self.plan(window, history, max_clock_uncertainty)
        for identity, lock in self.locks.items():
            if identity not in self.used_locks:
                # Absence from this horizon alone is not permission to erase a
                # secured lock. Coordination owns cancellation/expiry and pins.
                self.diagnose(
                    "lock_expired" if lock.end <= now else "lock_outside_projection",
                    assignment=identity,
                )
        players = tuple(PlayerProposal(
            player_id=player,
            layers=tuple(sorted(layers, key=lambda layer: (
                *stack_order(layer), layer.output_id, layer.assignment_id))),
        ) for player, layers in sorted(self.layers.items()))
        extent = max([horizon_end, *(layer.end for p in players for layer in p.layers)])
        if extent > horizon_end:
            self.diagnose("execution_extends_preparation_horizon")
        return Projection(
            now=now, horizon_end=horizon_end, valid_until=extent, players=players,
            acquisitions=tuple(acquisitions),
            diagnostics=tuple(self.diagnostics.values()),
            selections=tuple(self.selections),
            measurements=tuple(measurements),
            plan=tuple(planned),
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
) -> Projection:
    """Build complete-cycle proposals without changing any supplied state.

    Roadmap 5a (slice W1): the wall plan covers `plan_seconds` (never less than
    `horizon_seconds`, at most `limits.max_horizon_seconds`) from `now`, every Frame together;
    layers are proposed only for slots starting before now + `horizon_seconds` (the preparation
    horizon). `history` is the shown history and the locks' photos
    (central/infra/shown_history_store.py); `max_clock_uncertainty` sets the on-screen extents'
    guard (contracts/on_screen.py). Follow-ups B1: every slot is judged with `settings` (the
    photo fit settings) against `fit_target(frame_outputs[frame_id])` (frame box run fb-8: a
    Frame without a Frame output, or absent from it, has no shape, so its slots take no photo and
    keep the Frame's last still, else the fallback); a Scene
    without its own crop limit follows `settings.crop_limit`. Follow-up S3: `lines` answers the
    runtime's Source revisions and the top of each Scene's line (`_ProjectionBuilder._read_lines`);
    nothing reads a whole pool."""
    now = _finite(now, "now")
    horizon_seconds = _finite(horizon_seconds, "horizon_seconds")
    if horizon_seconds <= 0:
        raise PlanningError("horizon_seconds must be positive")
    plan_span = _finite(plan_seconds, "plan_seconds")
    if plan_span <= 0:
        raise PlanningError("plan_seconds must be positive")
    plan_span = max(plan_span, horizon_seconds)
    _bound(plan_span, limits.max_horizon_seconds, "horizon length")
    max_clock_uncertainty = _finite(max_clock_uncertainty, "max_clock_uncertainty")
    if max_clock_uncertainty < 0:
        raise PlanningError("max_clock_uncertainty must not be negative")
    window = PlanWindow(now=now, preparation_end=_finite(now + horizon_seconds, "horizon end"),
                        plan_end=_finite(now + plan_span, "plan end"))
    builder = _ProjectionBuilder(
        bindings_by_player=bindings_by_player,
        sources=lines.sources(runtime.planning_source_refs()), lines=lines,
        authored=authored_candidates, locks=locked_assignments, limits=limits,
        settings=settings, frame_outputs=frame_outputs, house=runtime.house,
    )
    try:
        # A turn starting up to the longest cross-fade lead past the plan's end may start to
        # show inside it.
        timeline = runtime.timeline(now, window.plan_end + runtime.longest_lead(),
                                    max_events=limits.max_events)
    except RuntimeBudgetExceeded as error:
        raise PlanningBudgetExceeded("Runtime event budget exceeded") from error
    for view in timeline:
        for intent in view.contributions:
            builder.add(intent, window)
    return builder.result(window, history, max_clock_uncertainty)
