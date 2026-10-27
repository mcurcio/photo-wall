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
    """The caller's input is inconsistent (a Frame bound twice, a pool keyed by another Source, a
    non-finite time). Raised within one Intent's `add`, it refuses that Intent alone
    (`intent_refused`); no budget raises it: a budget shortens the plan (`plan_shortened`)."""


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
    on an unbound Frame), for coordination's cues (`Coordinator._groups`): a cue is the showings
    of one root starting to show at one instant. A refused or cut showing is never a member, so
    no cue waits on a layer that will not be offered."""

    assignment_id: str
    root_id: str
    target: str
    shows_from: float
    interval_end: float


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


def _in_window(intent: Intent, window: PlanWindow) -> bool:
    """An Intent the plan horizon holds: on a Frame, showing before the plan's end, not over."""
    return (intent.kind != "actuator" and intent.shows_from < window.plan_end
            and intent.interval_end > window.now)


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
        self.diagnostics_dropped = 0
        self.selections: list[AssignmentSelection] = []
        self.seen: set[str] = set()
        self.used_locks: dict[str, Layer] = {}
        self.fixed: list[FixedShowing] = []
        self.pending: list[_Pending] = []
        self.pool_refs: dict[str, tuple[str, ...]] = {}
        self.cut: float | None = None
        self.cues: dict[str, CueMember] = {}
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
        )

    def add(self, intent: Intent, window: PlanWindow) -> None:
        """One Intent of the plan horizon: a lock or a black layer is fixed (a layer when it
        starts inside the preparation horizon); a media Intent becomes a wall-plan slot. Each
        starts when its photo starts to show (`Intent.shows_from`). One placed inside the
        preparation horizon (or a lock) is a cue member; one that raises adds nothing."""
        if not _in_window(intent, window):
            return
        identity = assignment_id(intent)
        if identity in self.seen:
            return
        self.seen.add(identity)
        self._place(intent, window, identity)
        if intent.shows_from < window.preparation_end or identity in self.locks:
            self.cues[identity] = CueMember(
                assignment_id=identity, root_id=intent.root_id, target=intent.target,
                shows_from=intent.shows_from, interval_end=intent.interval_end)

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
                start=intent.shows_from, end=intent.interval_end,
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
            start=intent.shows_from, end=intent.interval_end,
            fade_in=intent.fade_in_seconds, after_end=intent.after_end,
            target=self._target(binding.frame_id),
            crop_limit=crop_limit_of(intent.selection, self.settings),
            pool=pool, authored=intent.asset_refs))
        if not authored:
            self.pool_refs[pool] = intent.source_refs
        self.pending.append(pending)

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
            if inside:
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
) -> Projection:
    """Build complete-cycle proposals without changing any supplied state.

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
    budget: "runtime_events", "assignments" or "slots"), never cutting a lock. Only inconsistent
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
        settings=settings, frame_outputs=frame_outputs, house=runtime.house,
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
    return builder.result(window, history, max_clock_uncertainty)


def _refusal(error: Exception) -> str:
    """Why one Intent was refused, in the operator's words, for its `intent_refused` diagnostic:
    a PlanningError's own message (the Planner writes it), else a plain sentence naming only the
    error's type (another error's text can carry the values it judged)."""
    if isinstance(error, PlanningError):
        return str(error)
    return f"Central could not build its showing ({type(error).__name__})"
