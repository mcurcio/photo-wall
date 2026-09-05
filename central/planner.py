"""Pure bounded rolling media selection; no acquisition or execution authority."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

from pydantic import Field

from central.catalog import Candidate, CatalogSnapshot
from central.runtime import Intent, Runtime, RuntimeBudgetExceeded
from contracts.models import FrameProfile, Layer, Model, OutputBinding


class PlanningError(ValueError):
    """Invalid configuration or inconsistent input prevents a complete proposal."""


class PlanningBudgetExceeded(PlanningError):
    """No partial plan is returned after an explicit planning limit is exceeded."""


class PlannerLimits(Model):
    max_horizon_seconds: float = Field(default=3600, gt=0)
    max_cycle_seconds: float = Field(default=300, gt=0, le=300)
    max_events: int = Field(default=10000, ge=1)
    max_assignments: int = Field(default=4096, ge=1)
    max_sources: int = Field(default=128, ge=1)
    max_candidates: int = Field(default=10000, ge=1)
    max_acquisitions: int = Field(default=256, ge=1)
    max_diagnostics: int = Field(default=8192, ge=1)
    max_players: int = Field(default=128, ge=1)
    max_bindings: int = Field(default=256, ge=1)


class AcquisitionRequest(Model):
    asset_id: str
    assignment_ids: tuple[str, ...]
    earliest_start: float


class AssignmentSelection(Model):
    assignment_id: str
    asset_id: str | None = None
    locked: bool = False


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

    def for_player(self, player_id: str) -> PlayerProposal | None:
        return next((player for player in self.players if player.player_id == player_id), None)


def eligible(candidate: Candidate, profile: FrameProfile) -> bool:
    """Hard original-media policy, also applied to every authored alternative."""
    if candidate.kind == "image":
        return not (
            profile.height_px > profile.width_px
            and candidate.original_width > candidate.original_height
        )
    return profile.video and not (
        profile.diagonal_inches >= 40
        and min(candidate.original_width, candidate.original_height) < 1080
    )


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


def _variant_usable(candidate: Candidate, profile: FrameProfile) -> bool:
    variant = candidate.variant
    if variant is None:
        return False
    if candidate.kind == "image":
        return (
            variant.media_type in ("image/jpeg", "image/png")
            and variant.duration is None
            and not (profile.height_px > profile.width_px and variant.width > variant.height)
        )
    return (
        variant.media_type == "video/mp4" and variant.duration is not None
        and (profile.diagonal_inches < 40 or min(variant.width, variant.height) >= 1080)
    )


class _ProjectionBuilder:
    def __init__(
        self,
        *,
        bindings_by_player: Mapping[str, Sequence[OutputBinding]],
        snapshots: Mapping[str, CatalogSnapshot],
        authored: Mapping[str, Candidate],
        locks: Mapping[str, Layer],
        limits: PlannerLimits,
    ) -> None:
        self.limits, self.snapshots, self.authored, self.locks = limits, snapshots, authored, locks
        self.frames: dict[str, tuple[str, OutputBinding]] = {}
        self.layers: dict[str, list[Layer]] = {player: [] for player in bindings_by_player}
        self.diagnostics: dict[tuple, Diagnostic] = {}
        self.selections: list[AssignmentSelection] = []
        self.requests: dict[str, tuple[set[str], float]] = {}
        self.seen: set[str] = set()
        self.used_locks: set[str] = set()
        self.pools: dict[tuple, tuple[Candidate, ...]] = {}
        self._validate_inputs(bindings_by_player)

    def _validate_inputs(self, bindings: Mapping[str, Sequence[OutputBinding]]) -> None:
        _bound(len(bindings), self.limits.max_players, "Player count")
        _bound(sum(len(items) for items in bindings.values()), self.limits.max_bindings, "binding count")
        _bound(len(self.snapshots), self.limits.max_sources, "source count")
        _bound(
            len(self.authored) + sum(len(snapshot.candidates) for snapshot in self.snapshots.values()),
            self.limits.max_candidates, "candidate count",
        )
        _bound(len(self.locks), self.limits.max_assignments, "locked assignment count")
        for source, snapshot in self.snapshots.items():
            if snapshot.source_ref != source:
                raise PlanningError("catalog snapshot key does not match its immutable source reference")
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
            snapshot = self.snapshots.get(source)
            if snapshot is None:
                self.diagnose("source_missing", intent=intent, assignment=identity, source=source)
            elif snapshot.status != "ok":
                self.diagnose(
                    f"source_{snapshot.status}", intent=intent, assignment=identity, source=source
                )
            elif not snapshot.candidates:
                self.diagnose("source_empty", intent=intent, assignment=identity, source=source)

    def _pool(self, intent: Intent, profile: FrameProfile) -> tuple[Candidate, ...]:
        key = (intent.asset_refs, intent.source_refs, profile)
        if key in self.pools:
            return self.pools[key]
        if intent.asset_refs:
            candidates = [self.authored[ref] for ref in intent.asset_refs if ref in self.authored]
        else:
            # Newer snapshots win duplicate asset metadata deterministically;
            # sources remain immutable definitions while their results evolve.
            merged: dict[str, tuple[tuple[float, str], Candidate]] = {}
            for source in intent.source_refs:
                snapshot = self.snapshots.get(source)
                if snapshot is None or snapshot.status != "ok":
                    continue
                for candidate in snapshot.candidates:
                    version = (snapshot.refreshed_at, source)
                    if candidate.asset_id not in merged or version > merged[candidate.asset_id][0]:
                        merged[candidate.asset_id] = (version, candidate)
            candidates = sorted(
                (entry[1] for entry in merged.values()),
                key=lambda candidate: (-candidate.captured_at, candidate.asset_id),
            )
        pool = tuple(candidate for candidate in candidates if eligible(candidate, profile))
        self.pools[key] = pool
        return pool

    @staticmethod
    def _layer_fields(intent: Intent, binding: OutputBinding, identity: str) -> dict:
        return dict(
            assignment_id=identity, run_id=intent.run_id,
            output_id=binding.output_id, frame_id=binding.frame_id,
            binding_generation=binding.generation,
            start=intent.interval_start, end=intent.interval_end,
            media_origin=intent.interval_start,
            priority=intent.priority, root_order=intent.root_order,
            admission_order=intent.admission_order,
            opacity=intent.base_opacity,
            fade_in=intent.fade_in_seconds, fade_out=intent.fade_out_seconds,
        )

    def add(self, intent: Intent, now: float, horizon_end: float) -> None:
        if intent.kind == "actuator" or intent.interval_start >= horizon_end or intent.interval_end <= now:
            return
        _bound(len(intent.source_refs), self.limits.max_sources, "authored source reference count")
        _bound(len(intent.asset_refs), self.limits.max_candidates, "authored asset reference count")
        identity = assignment_id(intent)
        if identity in self.seen:
            return
        _bound(len(self.seen) + 1, self.limits.max_assignments, "assignment count")
        self.seen.add(identity)
        if intent.interval_end - intent.interval_start > self.limits.max_cycle_seconds:
            raise PlanningBudgetExceeded("cycle length exceeds supported preparation extent")
        if intent.fade_in_seconds + intent.fade_out_seconds > intent.interval_end - intent.interval_start:
            raise PlanningError("authored fades exceed their complete cycle interval")
        owner = self.frames.get(intent.target)
        if owner is None:
            self.diagnose("frame_unbound", intent=intent, assignment=identity)
            return
        player, binding = owner
        fields = self._layer_fields(intent, binding, identity)
        if intent.kind == "media":
            self._source_diagnostics(intent, identity)
        locked = self.locks.get(identity)
        if locked is not None:
            expected_kind = "black" if intent.kind == "black" else "media"
            arbitration = {"priority", "root_order", "admission_order"}
            if any(
                getattr(locked, field) != value
                for field, value in fields.items() if field not in arbitration
            ) or locked.presentation != expected_kind:
                self.diagnose("lock_stale_authority", intent=intent, assignment=identity)
                return
            self.used_locks.add(identity)
            self.layers[player].append(locked.model_copy(update={
                field: fields[field] for field in arbitration
            }))
            self.selections.append(AssignmentSelection(assignment_id=identity, locked=True))
            return
        if intent.kind == "black":
            self.layers[player].append(Layer(**fields, presentation="black"))
            return
        pool = self._pool(intent, binding.profile)
        if not pool:
            self.diagnose(
                "authored_no_eligible_alternative" if intent.asset_refs else "no_eligible_candidates",
                intent=intent, assignment=identity,
            )
            return
        candidate = pool[0] if intent.asset_refs else pool[intent.cycle_index % len(pool)]
        self.selections.append(AssignmentSelection(assignment_id=identity, asset_id=candidate.asset_id))
        if candidate.variant is None:
            if candidate.asset_id not in self.requests:
                _bound(len(self.requests) + 1, self.limits.max_acquisitions, "acquisition count")
                self.requests[candidate.asset_id] = (set(), intent.interval_start)
            assignments, earliest = self.requests[candidate.asset_id]
            assignments.add(identity)
            self.requests[candidate.asset_id] = (assignments, min(earliest, intent.interval_start))
            self.diagnose("preparation_pending", intent=intent, assignment=identity)
            return
        if not _variant_usable(candidate, binding.profile):
            self.diagnose("variant_incompatible", intent=intent, assignment=identity)
            return
        self.layers[player].append(Layer(**fields, presentation="media", variant=candidate.variant))

    def result(self, now: float, horizon_end: float) -> Projection:
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
                layer.start, layer.priority, layer.root_order, layer.admission_order,
                layer.output_id, layer.assignment_id,
            ))),
        ) for player, layers in sorted(self.layers.items()))
        extent = max([horizon_end, *(layer.end for p in players for layer in p.layers)])
        if extent > horizon_end:
            self.diagnose("execution_extends_preparation_horizon")
        return Projection(
            now=now, horizon_end=horizon_end, valid_until=extent, players=players,
            acquisitions=tuple(AcquisitionRequest(
                asset_id=asset, assignment_ids=tuple(sorted(assignments)), earliest_start=earliest,
            ) for asset, (assignments, earliest) in sorted(self.requests.items())),
            diagnostics=tuple(self.diagnostics.values()),
            selections=tuple(self.selections),
        )


def project(
    runtime: Runtime,
    now: float,
    *,
    bindings_by_player: Mapping[str, Sequence[OutputBinding]],
    catalog_snapshots: Mapping[str, CatalogSnapshot],
    authored_candidates: Mapping[str, Candidate],
    locked_assignments: Mapping[str, Layer],
    horizon_seconds: float = 300,
    limits: PlannerLimits = PlannerLimits(),
) -> Projection:
    """Build complete-cycle proposals without changing any supplied state."""
    now = _finite(now, "now")
    horizon_seconds = _finite(horizon_seconds, "horizon_seconds")
    if horizon_seconds <= 0:
        raise PlanningError("horizon_seconds must be positive")
    _bound(horizon_seconds, limits.max_horizon_seconds, "horizon length")
    horizon_end = _finite(now + horizon_seconds, "horizon end")
    builder = _ProjectionBuilder(
        bindings_by_player=bindings_by_player, snapshots=catalog_snapshots,
        authored=authored_candidates, locks=locked_assignments, limits=limits,
    )
    try:
        timeline = runtime.timeline(now, horizon_end, max_events=limits.max_events)
    except RuntimeBudgetExceeded as error:
        raise PlanningBudgetExceeded("Runtime event budget exceeded") from error
    for view in timeline:
        for intent in view.contributions:
            builder.add(intent, now, horizon_end)
    return builder.result(now, horizon_end)
