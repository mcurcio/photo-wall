"""Persisted offers, byte locks and revocable group execution authority.

No upstream transport, conversion or renderer work occurs in these transactions.
See docs/module-coordination.md for lock order and conservative offer leases.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError

from central.actor import Actor
from central.cue_start import CueReadiness, grantable, starts_whole
from central.db import Database, DatabaseTransactionClock
from central.everyday.model import (
    CoverCause,
    EverydayRefusal,
    Moved,
    Pause,
    PausedFrame,
    PauseRefusal,
    PauseStayed,
    SkipKind,
    SkipOutcome,
    SkipScope,
    Stayed,
    StayReason,
)
from central.everyday.policy import (
    carried_on,
    cover_drawn,
    cover_for,
    draws,
    hold_for,
    pause_refusal,
    photo_of,
    showing_at,
    stays,
    the_shared_instant,
)
from central.everyday.ports import FrameFacts, FrameFactsReader, SkipRecord
from central.everyday.views import (
    CarryOnAnswer,
    LastShowing,
    NeverShowAnswer,
    NeverShowListView,
    PauseAnswer,
    SkipAnswer,
)
from central.infra.display_store import frame_outputs_in
from central.infra.fit_settings_store import PgFitSettings
from central.infra.never_show_store import PgNeverShow
from central.infra.pause_store import PgPauses
from central.infra.planning_outcome_store import PgPlanningOutcome
from central.infra.player_state_wakes import notify_if_written_in
from central.infra.shown_history_store import PgShownHistory
from central.infra.skip_store import PgSkips
from central.installation_models import OutputInterruption
from central.installation_ports import InstallationSessions
from central.installation_repository import PostgresInstallationRepository
from central.media_ports import CoordinationMedia
from central.media_repository import MediaRepository
from central.planner import (
    PLAN_SECONDS,
    PlannerLimits,
    Projection,
    eligible,
    project,
    same_execution,
)
from central.planning_outcome import PlanningOutcomes
from central.registry import RegistryError
from central.runtime import PAUSE_RANK, Scene
from central.runtime_store import RuntimeStore
from central.scheduler import scheduler_failure
from central.transaction_locks import COORDINATION_LOCK, holds_runtime_locks_in
from central.wall_plan.assignment import Reservation, frame_clock_uncertainty
from central.wall_plan.continuation import held_reservations
from central.wall_plan.fit import FitTarget, fit_target
from central.wall_plan.order import PhotoShowing
from central.wall_plan.ports import FitSettingsStore, ShownHistory
from central.wall_plan.selection import SceneSelection, crop_limit_of
from contracts.continuation import continuation_source
from contracts.models import (
    PREPARE_SECONDS,
    Commit,
    IdentifyOutput,
    Layer,
    Model,
    Observation,
    Plan,
    PlayerConfiguration,
    Readiness,
    Revocation,
    UnfilledSpan,
)
from contracts.on_screen import Extent
from contracts.time import DRIFT_RATE, Clock

LOG = logging.getLogger(__name__)


class CoordinationLimits(Model):
    horizon_seconds: float = Field(default=300, gt=0, le=3600)
    renewal_seconds: float = Field(default=30, gt=0, le=60)
    max_uncertainty: float = Field(default=0.1, gt=0, le=1)
    max_offers: int = Field(default=64, ge=1, le=256)
    # Roadmap 5a (slice W1): how far ahead the wall plan reaches, every Frame together; layers
    # are still offered only inside `horizon_seconds`.
    plan_seconds: float = Field(default=PLAN_SECONDS, gt=0, le=PLAN_SECONDS)


class CoordinationError(RegistryError):
    pass


@dataclass(frozen=True)
class StepFailure:
    """One coordinator step that failed this tick, and why (`scheduler_failure`: the exception's
    type and where, never its message)."""

    step: Literal["runtime", "planning", "settle"]
    reason: str


@dataclass(frozen=True)
class Advance:
    """One tick of the coordinator: the projection planning offered (None when planning failed),
    every step that failed, and `wake_at`: the earliest instant its state changes by itself (the
    Runtime's next event, the planner's next renewal boundary while a Player has layers, the next
    due cue; None when nothing is due). The scheduler sleeps until then (central/scheduler.py)."""

    projection: Projection | None
    failures: tuple[StepFailure, ...]
    wake_at: float | None = None


@dataclass(frozen=True)
class FramePlan:
    """What a Player's newest valid plan offer holds for one Frame (`Coordinator.frame_plans_in`):
    its unfilled spans not yet over, its media layers not yet over, and how many of those layers
    the Player's readiness for that offer names prepared or failed."""

    unfilled: tuple[UnfilledSpan, ...]
    layers: int
    prepared: int
    failed: int


def next_due_cue_in(conn, now: float) -> float | None:
    """The next instant after `now` at which settle may grant a cue by itself: a cue entering its
    preparation window (`starts_at - PREPARE_SECONDS`, `_commit_due`) or a pending cue's deadline
    (its grant stops being all-or-nothing, central/cue_start.py `starts_whole`); None when none is
    due. A Player's readiness grants earlier, and its commit wakes the scheduler itself."""
    row = conn.execute(
        "SELECT min(at) AS at FROM (SELECT starts_at-%(prepare)s AS at FROM coordination_groups "
        "WHERE status!='skipped' AND valid_until>%(now)s UNION ALL SELECT deadline FROM "
        "coordination_groups WHERE status='pending' AND valid_until>%(now)s) due WHERE at>%(now)s",
        {"prepare": PREPARE_SECONDS, "now": now}).fetchone()
    return row["at"]


def _renewal_at(projection: Projection, quantum: float) -> float | None:
    """The planner's next renewal boundary (its horizon moves on there, `Coordinator._plan`), while
    some Player is offered layers; None when none is: an empty plan has nothing to renew early."""
    if not any(proposal.layers for proposal in projection.players):
        return None
    return (math.floor(projection.now / quantum) + 1) * quantum


def identity(prefix: str, parts) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return prefix + hashlib.sha256(encoded.encode()).hexdigest()[:40]


def _stored_configuration(player_id: str, stored) -> PlayerConfiguration | None:
    """A Player's last configuration as player_show_state holds it, or None when it no longer
    reads as the current contract (a shape an earlier release wrote): it is then regenerated under
    the next revision, so one stale document never stops the wall. The warning names the table,
    the Player and the failing fields, never the stored content."""
    try:
        return PlayerConfiguration.model_validate(stored)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        LOG.warning("player_show_state: stored configuration for player %s no longer validates "
                    "(%s); regenerating it", player_id, ", ".join(fields) or "document")
        return None


def _refusal_code(exc: Exception) -> str:
    """The code `_left_out` names for one Player left out: a registry refusal's own code, else the
    failure's type and where (`scheduler_failure`), never its message."""
    return exc.code if isinstance(exc, RegistryError) else scheduler_failure(exc)


def _left_out(player_id: str, code: str, *, failed: bool = True) -> None:
    """One Player left out of a planning pass, logged (`offer_unavailable`): `failed` when its
    configuration, lock or offer failed (a warning), else an offer withheld by rule
    (`plan_shortened`, `offer_backpressure`). Nothing is stored: the next pass decides afresh,
    and what the pass itself could not plan is the planning outcome (central/planning_outcome.py)."""
    LOG.log(logging.WARNING if failed else logging.INFO,
            "offer_unavailable: player %s left out of this pass (%s)", player_id, code)


def _withdraws(latest: Plan, proposal, projection: Projection, now: float) -> bool:
    """Whether a cut plan (`Projection.cut`) would withdraw a layer the latest offer still
    holds past the cut: a budget shortens what is planned anew, never what was offered, so the
    latest offer stands until the plan reaches that far again or the offer expires."""
    if projection.cut is None:
        return False
    proposed = {layer.assignment_id for layer in proposal.layers}
    return any(layer.end > now and layer.start >= projection.cut
               and layer.assignment_id not in proposed for layer in latest.layers)


def _may_hold(offer, now: float) -> bool:
    """Decision 0032: whether an expired offer's Player may still be playing its continuations:
    its revision is at or past the one the Player's latest readiness names, and one of its
    continuations has no known end or ends after now. Such an offer is kept (its reservations
    hold, `Coordinator._held_in`)."""
    readiness = offer["readiness"]
    if readiness is not None and offer["revision"] < readiness["revision"]:
        return False
    try:
        plan = Plan.model_validate(offer["manifest"])
    except ValidationError:
        return False
    return any(c.until is None or c.until > now for c in plan.continuations)


def _acknowledged_revision_in(conn, player_id: str, epoch: int) -> int:
    """The revision the Player's latest readiness in `epoch` names; 0 when it has reported none."""
    row = conn.execute(
        "SELECT (readiness->>'revision')::bigint AS revision FROM player_show_state "
        "WHERE player_id=%s AND authority_epoch=%s AND readiness IS NOT NULL",
        (player_id, epoch)).fetchone()
    return 0 if row is None or row["revision"] is None else row["revision"]


def _unacknowledged(offers: list[Plan], acknowledged: int) -> list[Plan]:
    """The offers past the revision the Player's readiness names: what backpressure counts. It
    guards against a Player that does not read its offers; an offer it has read past is not one
    (0036 slice 3, build step 0: an hour-long layer keeps every revision valid for the hour, so
    counting every unexpired offer starved a reading Player within minutes)."""
    return [offer for offer in offers if offer.revision > acknowledged]


def _superseded(offer: Plan, acknowledged: Plan, now: float) -> bool:
    """Whether an unexpired offer older than the one the Player acknowledged holds nothing that
    offer does not: every layer not yet ended is in it as the same execution. Its continuations
    hold nothing either: the Player read past it (`_may_hold`, decision 0032). Cleanup deletes
    such an offer (`Coordinator._cleanup`), so `plan_offers` holds no hour of revisions."""
    if offer.revision >= acknowledged.revision:
        return False
    kept = {layer.assignment_id: layer for layer in acknowledged.layers}
    return all(layer.assignment_id in kept and same_execution(layer, kept[layer.assignment_id])
               for layer in offer.layers if layer.end > now)


def _skip_answer(record: SkipRecord, *, duplicate: bool) -> SkipAnswer:
    return SkipAnswer(request_id=record.request_id,
                      kind=SkipKind.PREVIOUS if record.cause == CoverCause.PREVIOUS.value else SkipKind.NEXT,
                      at=record.outcome.at, duplicate=duplicate,
                      moved=tuple(Moved(frame_id=cover.frame_id) for cover in record.outcome.covers),
                      stayed=record.outcome.stayed)


def _scene_of(frame: FrameFacts, showing: Layer | None) -> str:
    """The Scene a Frame's showing layer is of: its Intent's, else that of another layer of its
    Run (a cover has no Intent of its own); its Run's id when none is known."""
    if showing is None:
        return "unknown"
    origin = frame.origins.get(showing.assignment_id) or next(
        (found for layer in frame.layers if layer.run_id == showing.run_id
         and (found := frame.origins.get(layer.assignment_id)) is not None), None)
    return showing.run_id if origin is None else origin.scene_id


def _membership(layers, groups: dict[str, str]) -> dict[str, str]:
    """Each layer's cue group: only a layer that awaits a grant is a cue member (decision 0032),
    so a layer with no group starts at its instant with no Commit."""
    return {layer.assignment_id: groups[layer.assignment_id] for layer in layers
            if layer.assignment_id in groups}


class OutputLossLedger:
    """The one home of an Output loss's rule (`central.displays.ports.OutputLosses`): Runtime state
    the Coordinator reads (`_commit_due`, `output_interruptions_in`). Stateless, so the worker that
    judges the display line builds it with no Coordinator. Both calls run in the caller's
    transaction, which holds Coordination then Runtime (`acquire_runtime_locks`) before any row lock
    of its own; ValueError("output_loss_runtime_locks_required") otherwise."""

    def lost_in(self, conn: Any, *, player_id: str, output_id: str, cause_layer: Literal["display_host"],
                cause_kind: Literal["output_disconnected"], detail: Mapping[str, object], at: float) -> bool:
        """One unresolved `output_losses` row for the Frame bound to (player_id, output_id) now (its
        generation and configuration revision, the player's authority epoch), and every
        execution_commits row of an offer layer on that exact binding marked invalid. False, writing
        nothing, when no Frame is bound there or that binding's loss is already unresolved."""
        _require_runtime_locks(conn)
        bound = conn.execute(
            "SELECT f.id AS frame_id,f.generation,f.configuration_revision,p.authority_epoch FROM frames f "
            "JOIN players p ON p.id=f.player_id WHERE f.player_id=%s AND f.output_id=%s",
            (player_id, output_id)).fetchone()
        if bound is None:
            return False
        key = (player_id, bound["authority_epoch"], output_id, bound["frame_id"], bound["generation"])
        existing = conn.execute("SELECT resolved_at FROM output_losses WHERE player_id=%s AND authority_epoch=%s "
                                "AND output_id=%s AND frame_id=%s AND binding_generation=%s", key).fetchone()
        if existing is not None and existing["resolved_at"] is None:
            return False
        conn.execute("INSERT INTO output_losses(player_id,authority_epoch,output_id,frame_id,binding_generation,"
                     "configuration_revision,cause_layer,cause_kind,detail,interrupted_at,created_at,created_by,"
                     "changed_at,changed_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'node',%s,'node') "
                     "ON CONFLICT(player_id,authority_epoch,output_id,frame_id,binding_generation) DO UPDATE "
                     "SET configuration_revision=EXCLUDED.configuration_revision,cause_layer=EXCLUDED.cause_layer,"
                     "cause_kind=EXCLUDED.cause_kind,detail=EXCLUDED.detail,interrupted_at=EXCLUDED.interrupted_at,"
                     "resolved_at=NULL,changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by",
                     (*key, bound["configuration_revision"], cause_layer, cause_kind, Jsonb(dict(detail)),
                      at, at, at))
        # Invalidate only layers on this exact Output binding, across retained revisions.
        offers = conn.execute("SELECT revision,manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s",
                              (player_id, bound["authority_epoch"])).fetchall()
        for row in offers:
            plan = Plan.model_validate(row["manifest"])
            assignments = [layer.assignment_id for layer in plan.layers if layer.output_id == output_id
                           and layer.frame_id == bound["frame_id"] and layer.binding_generation == bound["generation"]]
            if assignments:
                conn.execute("UPDATE execution_commits SET valid=FALSE,changed_at=%s,changed_by='central' "
                             "WHERE player_id=%s AND authority_epoch=%s AND revision=%s AND assignment_id=ANY(%s)",
                             (at, player_id, bound["authority_epoch"], row["revision"], assignments))
        return True

    def restored_in(self, conn: Any, *, player_id: str, output_id: str, at: float) -> bool:
        """Resolve every unresolved loss of (player_id, output_id), whatever Frame or generation it
        names (a rebind while disconnected leaves none behind); False when there was none."""
        _require_runtime_locks(conn)
        return bool(conn.execute(
            "UPDATE output_losses SET resolved_at=%s,changed_at=%s,changed_by='node' "
            "WHERE player_id=%s AND output_id=%s AND resolved_at IS NULL RETURNING frame_id",
            (at, at, player_id, output_id)).fetchall())


def _require_runtime_locks(conn: Any) -> None:
    if not holds_runtime_locks_in(conn):
        raise ValueError("output_loss_runtime_locks_required")


class Coordinator:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        limits: CoordinationLimits | None = None,
        *,
        media: CoordinationMedia | None = None,
        installation: InstallationSessions | None = None,
        shown: ShownHistory | None = None,
        fit: FitSettingsStore | None = None,
        planning: PlanningOutcomes | None = None,
    ):
        self.db, self.clock = db, clock
        self.limits = limits or CoordinationLimits()
        self.runtime = RuntimeStore(db, clock)
        self.media: CoordinationMedia = media or MediaRepository(db, clock, times=DatabaseTransactionClock())
        self.installation = installation or PostgresInstallationRepository(clock)
        # Roadmap 5a: what each offered layer shows and which were displayed (shown history).
        self.shown: ShownHistory = shown or PgShownHistory()
        # Follow-ups B1: the photo fit settings, read once per planning pass and authoring check.
        self.fit: FitSettingsStore = fit or PgFitSettings(db, times=DatabaseTransactionClock())
        # What the last planning pass could not do, for the operator (central/planning_outcome.py).
        self.planning: PlanningOutcomes = planning or PgPlanningOutcome(DatabaseTransactionClock())
        # Decision 0036 (slice 3): the everyday actions' covers and the Never show list.
        self.skips = PgSkips()
        self.never_show_list = PgNeverShow()
        self.pauses = PgPauses()

    @staticmethod
    def _scene_contributions(scene: Scene):
        pending = [scene]
        while pending:
            current = pending.pop()
            yield from current.contributions
            yield from current.outro_contributions
            pending.extend(child.scene for child in current.children)

    @classmethod
    def _authored_scene_refs(cls, scene: Scene) -> tuple[set[str], bool]:
        asset_refs: set[str] = set()
        has_source_refs = False
        for contribution in cls._scene_contributions(scene):
            asset_refs.update(contribution.asset_refs)
            has_source_refs = has_source_refs or bool(contribution.source_refs)
        return asset_refs, has_source_refs

    def _authored_scene_targets(self, conn, scene: Scene) -> dict[str, FitTarget]:
        """The `fit_target` of each Frame the Scene authors photos onto (frame box run fb-8).
        CoordinationError("unknown_frame", 404) for a Frame that does not exist;
        RegistryError("authored_frame_size_unknown", 409) for one without a Frame output (no
        known screen: no shape, so no photo can be judged for it)."""
        frame_ids = {
            contribution.target.removeprefix("frame:")
            for contribution in self._scene_contributions(scene)
            if contribution.asset_refs
        }
        outputs = {output.frame_id: output for output in frame_outputs_in(conn)
                   if output.frame_id in frame_ids}
        if len(outputs) != len(frame_ids):
            raise CoordinationError("unknown_frame", 404)
        targets = {frame_id: fit_target(output) for frame_id, output in outputs.items()}
        sized = {frame_id: target for frame_id, target in targets.items() if target is not None}
        if len(sized) != len(targets):
            raise RegistryError("authored_frame_size_unknown", 409)
        return sized

    def _validate_authored_scene(
        self, conn, scene: Scene, asset_ids: tuple[str, ...], targets: dict[str, FitTarget]
    ) -> None:
        candidates = self.media.authored_candidates_in(conn, asset_ids)
        settings = self.fit.fit_settings_in(conn)
        crop_limit = crop_limit_of(scene.selection, settings)
        for contribution in self._scene_contributions(scene):
            if not contribution.asset_refs:
                continue
            target = targets[contribution.target.removeprefix("frame:")]
            if not any(
                eligible(candidates[asset_id], target, crop_limit, settings)
                for asset_id in contribution.asset_refs
            ):
                raise RegistryError("authored_incompatible", 409)

    def configure_authored_scene(
        self, scene: Scene, source_ref: str, asset_ids: tuple[str, ...], *, by: Actor
    ) -> dict:
        """Atomically author media refs and save their Scene definition (`Runtime.save_scene`):
        the answer is the `SceneOnWall` with the authored refs (`asset_refs`, `created`)."""
        return self._author_scene("save_scene", scene, source_ref, asset_ids, by=by)

    def configure_authored_scene_and_apply_now(
        self, scene: Scene, source_ref: str, asset_ids: tuple[str, ...], *, by: Actor
    ) -> dict:
        """As `configure_authored_scene`, saved and applied at once in the same transaction
        (`Runtime.save_scene_and_apply_now`)."""
        return self._author_scene("save_scene_and_apply_now", scene, source_ref, asset_ids, by=by)

    def _author_scene(
        self, command: Literal["save_scene", "save_scene_and_apply_now"], scene: Scene,
        source_ref: str, asset_ids: tuple[str, ...], *, by: Actor,
    ) -> dict:
        try:
            requested = tuple(asset_ids)
            requested_set = set(requested)
        except (TypeError, ValueError) as exc:
            raise RegistryError("invalid_authored_scene", 422) from exc
        scene_refs, has_source_refs = self._authored_scene_refs(scene)
        if (
            not requested
            or len(requested) != len(requested_set)
            or requested_set != scene_refs
            or has_source_refs
        ):
            raise RegistryError("invalid_authored_scene", 422)
        with self._transaction() as conn, self.runtime.edit(conn, by=by, command=command) as runtime:
            # Keep the existing Runtime -> Frame -> Media order.
            targets = self._authored_scene_targets(conn, scene)
            authored = self.media.author_candidates_in(conn, source_ref, requested)
            self._validate_authored_scene(conn, scene, requested, targets)
            # One Runtime command, at the post-lock cut on Central's clock (as
            # RuntimeStore.command_current).
            on_wall = getattr(runtime, command)(scene, self.clock.utc())
            return {**on_wall.model_dump(mode="json"), **authored}

    @contextmanager
    def _transaction(self):
        with self.db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            yield conn
            # A Player's state can change only here: its sockets are woken when this commits,
            # if it wrote (central/infra/player_state_wakes.py).
            notify_if_written_in(conn)

    @contextmanager
    def serialized_runtime_read(self):
        """Hold Coordination and Runtime locks while inspecting current Runtime state."""
        with self._transaction() as conn:
            yield conn, self.runtime.read_locked(conn)

    def _players(self, conn):
        return self.installation.active_sessions_in(conn)

    def _configurations(self, conn, *, report: bool, by: Actor) -> dict[str, PlayerConfiguration]:
        """Each active Player's configuration. The Player is the unit of isolation: one whose
        configuration fails (its authority went stale since its session was read, or anything
        else) is left out of this pass alone, its writes rolled back to its own savepoint
        (`_left_out` naming the code when `report`), never the whole wall. Each is read with
        `lock=False`: it row-locks the session, Frames and bindings only when the stored
        configuration must change (the rule `delivery` follows), so a pass over an unchanged wall
        takes no row lock and no transaction id (decision 0034, class D). Callers: `_plan`,
        `_settle` (`by` "scheduler"), `readiness` ("central")."""
        configurations = {}
        for player in self._players(conn):
            try:
                with conn.transaction():
                    configurations[player["id"]] = self._configuration(
                        conn, player["id"], player["authority_epoch"], lock=False, by=by)
            except Exception as exc:   # noqa: BLE001 - the per-Player bulkhead
                if report:
                    _left_out(player["id"], _refusal_code(exc))
        return configurations

    def _configuration(self, conn, player_id: str, epoch: int, *, lock: bool = True,
                       by: Actor = "central") -> PlayerConfiguration:
        """The Player's configuration, kept in its show state (`player_show_state`) and written only
        when it changed; a configuration of a newer epoch clears the show state's readiness and
        observation mark, which were the older epoch's. `lock=False` (a delivery: a read) reads it
        without share locks and takes them only when the stored row must change, so a state poll
        of an unchanged configuration row-locks and writes nothing (2026-11-16: a WAL flush per
        poll). `by`: "scheduler" from the tick, else "central" (Coordination's own derivation)."""
        for locking in ((False, True) if not lock else (True,)):
            observed = self.installation.configuration_in(conn, player_id, epoch, lock=locking)
            if observed is None:
                raise CoordinationError("stale_authority", 403)
            content = {
                "bindings": [b.model_dump(mode="json") for b in observed["bindings"]],
                "enabled_outputs": [b.output_id for b in observed["execution_bindings"]],
            }
            old = conn.execute(
                "SELECT authority_epoch,configuration_revision AS revision,configuration "
                "FROM player_show_state WHERE player_id=%s", (player_id,)
            ).fetchone()
            revision = 1
            if old and old["authority_epoch"] == epoch and old["configuration"] is not None:
                previous = _stored_configuration(player_id, old["configuration"])
                revision = old["revision"] + int(
                    previous is None
                    or content != previous.model_dump(mode="json",
                                                      include={"bindings", "enabled_outputs"})
                )
            configuration = PlayerConfiguration(
                player_id=player_id, authority_epoch=epoch, configuration_revision=revision,
                **content
            )
            document = configuration.model_dump(mode="json")
            # Written only when it changed: an unchanged row is never rewritten (design rule 2).
            changed = old is None or (old["authority_epoch"], old["revision"],
                                      old["configuration"]) != (epoch, revision, document)
            if not changed:
                return configuration
        now = self.clock.utc()
        # Another epoch's readiness and observation mark are not this epoch's: cleared with it.
        newer = "" if old is not None and old["authority_epoch"] == epoch else (
            ",readiness_sequence=NULL,readiness=NULL,readiness_received_at=NULL,"
            "observed_sequence=NULL")
        conn.execute(
            "INSERT INTO player_show_state(player_id,authority_epoch,configuration_revision,"
            "configuration,created_at,created_by,changed_at,changed_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(player_id) DO UPDATE SET authority_epoch=EXCLUDED.authority_epoch,"
            "configuration_revision=EXCLUDED.configuration_revision,"
            "configuration=EXCLUDED.configuration,changed_at=EXCLUDED.changed_at,"
            "changed_by=EXCLUDED.changed_by" + newer,
            (player_id, epoch, revision, Jsonb(document), now, by, now, by),
        )
        return configuration

    def configuration(self, player_id: str, epoch: int) -> PlayerConfiguration:
        with self._transaction() as conn:
            return self._configuration(conn, player_id, epoch)

    @staticmethod
    def _authorized(layer: Layer, configuration: PlayerConfiguration) -> bool:
        return configuration.authorizes(layer)

    def _offers(
        self, conn, configurations: dict[str, PlayerConfiguration]
    ) -> dict[str, list[Plan]]:
        result = {p: [] for p in configurations}
        for row in conn.execute(
            "SELECT manifest FROM plan_offers WHERE valid_until>%s ORDER BY revision",
            (self.clock.utc(),),
        ).fetchall():
            try:
                plan = Plan.model_validate(row["manifest"])
            except ValidationError:
                # One stored offer that no longer reads (a shape an earlier release wrote) is
                # left out alone; its Player is offered a fresh revision. Never the content.
                LOG.warning("plan_offers: a stored offer no longer validates; leaving it out")
                continue
            config = configurations.get(plan.player_id)
            # A restarted Player loses execution authority, not the exact
            # content identity of an unexpired, possibly secured assignment.
            if config:
                result[plan.player_id].append(plan)
        return result

    def _locks(self, conn, configs, offers) -> tuple[dict[str, Layer], dict[str, str]]:
        """The content locks of this pass, and the Players left out of it with their code. The
        Player is the unit of isolation: a Player with a stored lock that no longer reads, or
        whose lock disagrees with another on the same assignment, is left out alone (its offers
        stand), never the whole wall."""
        refused: dict[str, str] = {}
        layers = [
            (p, layer) for p, plans in offers.items() for plan in plans for layer in plan.layers
        ]
        for row in conn.execute(
            "SELECT player_id,authority_epoch,layer FROM assignment_locks WHERE valid_until>%s",
            (self.clock.utc(),),
        ).fetchall():
            if row["player_id"] not in configs:
                continue
            try:
                layers.append((row["player_id"], Layer.model_validate(row["layer"])))
            except ValidationError:
                refused[row["player_id"]] = "invalid_content_lock"
        claims: dict[str, list[tuple[str, Layer]]] = {}
        for player, layer in layers:
            if layer.end <= self.clock.utc() or not self._authorized(layer, configs[player]):
                continue
            claims.setdefault(layer.assignment_id, []).append((player, layer))
        for claim in claims.values():
            first = claim[0][1]
            if any(not same_execution(first, layer) for _, layer in claim[1:]):
                for player, _ in claim:
                    refused[player] = "inconsistent_content_lock"
        result = {}
        for assignment, claim in claims.items():
            kept = [layer for player, layer in claim if player not in refused]
            if not kept:
                continue
            # Decision 0036: a cut is final, so the earliest offered wins over a copy without it
            # (a secured lock's row keeps the layer as first secured); a lock cut and over is gone.
            cut = min((layer.cut_at for layer in kept if layer.cut_at is not None), default=None)
            lock = kept[-1] if cut is None else next(layer for layer in kept if layer.cut_at == cut)
            if lock.drawn_end > self.clock.utc():
                result[assignment] = lock
        return result, refused

    def _groups(self, conn, projection: Projection, now, horizon_end,
                configurations) -> dict[str, str]:
        owners = {
            f"frame:{b.frame_id}": (p, c.authority_epoch, b.output_id, b.generation)
            for p, c in configurations.items()
            for b in c.bindings
            if b.output_id in c.enabled_outputs
        }
        cues: dict[tuple, dict] = {}
        # A cue starts when its layers do: as each photo starts to show (`Intent.shows_from`, a
        # cross-fade's lead before its turn). Its members are the showings the Planner placed
        # inside the preparation horizon (`Projection.cues`): the Planner's one timeline walk,
        # its window and its cut, so no cue waits on a layer the Planner did not place.
        for member in projection.cues:
            key = (member.root_id, member.shows_from)
            cue = cues.setdefault(key, {"members": {}, "ends": {}, "end": member.interval_end})
            cue["end"] = max(cue["end"], member.interval_end)
            cue["members"][member.assignment_id] = owners.get(member.target)
            cue["ends"][member.assignment_id] = member.interval_end
        assignments = {}
        cue_keys = {identity("cue-", list(key)) for key in cues}
        # A future cue is a forecast until it starts; one the timeline no longer holds is
        # withdrawn now.
        for orphan in conn.execute(
            "SELECT id,cue_key FROM coordination_groups WHERE status IN ('pending','committed') "
            "AND starts_at>%s AND starts_at<%s ORDER BY starts_at,id",
            (now, horizon_end),
        ).fetchall():
            if orphan["cue_key"] not in cue_keys:
                self._skip_group(conn, orphan["id"], now)
        for (root, starts), cue in cues.items():
            cue_key = identity("cue-", [root, starts])
            previous = conn.execute(
                "SELECT * FROM coordination_groups WHERE cue_key=%s "
                "ORDER BY cohort_sequence DESC LIMIT 1",
                (cue_key,),
            ).fetchone()
            superseding = False
            # A skipped cohort is over: a cue the timeline still holds takes a fresh one.
            if previous and previous["status"] != "skipped":
                grows = not cue["members"].keys() <= previous["members"].keys()
                moved = not grows and any(
                    (tuple(previous["members"][a]) if previous["members"][a] else None) != owner
                    for a, owner in cue["members"].items()
                )
                if starts > now and (moved or cue["members"].keys() != previous["members"].keys()):
                    # An edit rewrote a cue that has not started: a fresh cohort replaces it,
                    # and skipping the old one withdraws any commit it had already granted.
                    self._skip_group(conn, previous["id"], now)
                    superseding = True
                elif grows:
                    # A started cue that grows takes the new member in; what it granted stands.
                    conn.execute(
                        "UPDATE coordination_groups SET members=%s,member_ends=%s,valid_until=%s,"
                        "changed_at=%s,changed_by='scheduler' WHERE id=%s",
                        (
                            Jsonb({**previous["members"], **cue["members"]}),
                            Jsonb({**previous["member_ends"], **cue["ends"]}),
                            max(previous["valid_until"], cue["end"]),
                            now,
                            previous["id"],
                        ),
                    )
                    assignments.update({a: previous["id"] for a in cue["members"]})
                    continue
                elif not moved:
                    assignments.update({a: previous["id"] for a in cue["members"]})
                    continue
            # The previous cohort names the new one, so a forecast that returns to an
            # earlier membership never collides with that earlier, skipped cohort.
            group_id = identity(
                "group-", [cue_key, cue["members"], *([previous["id"]] if previous else [])]
            )
            # A late join keeps preparation grace. So does a superseding cohort: a Player that
            # fetches the new plan only after the start may already be playing the old commit,
            # and must be able to commit the new cohort rather than be revoked at the start.
            if starts <= now or superseding:
                deadline = max(starts, now) + PREPARE_SECONDS
            else:
                deadline = starts
            conn.execute(
                "INSERT INTO coordination_groups(id,members,starts_at,deadline,valid_until,status,"
                "cue_key,member_ends,created_at,created_by,changed_at,changed_by) "
                "VALUES(%s,%s,%s,%s,%s,'pending',%s,%s,%s,'scheduler',%s,'scheduler') "
                "ON CONFLICT(id) DO NOTHING",
                (
                    group_id,
                    Jsonb(cue["members"]),
                    starts,
                    deadline,
                    cue["end"],
                    cue_key,
                    Jsonb(cue["ends"]),
                    now,
                    now,
                ),
            )
            assignments.update({a: group_id for a in cue["members"]})
        return assignments

    def _skip_group(self, conn, group_id: str, now: float) -> None:
        """Skip a group the timeline no longer holds (superseded: its cue was withdrawn or rewritten
        before it started) and withdraw its grants. Its revocation's number is the next of
        `coordination_skip_sequence`, so a later skip's is always higher (the Player ignores a
        number at or below the last it took)."""
        conn.execute(
            "UPDATE coordination_groups SET status='skipped',"
            "skip_sequence=nextval('coordination_skip_sequence'),changed_at=%s,"
            "changed_by='scheduler' WHERE id=%s",
            (now, group_id),
        )
        conn.execute("UPDATE execution_commits SET valid=FALSE,changed_at=%s,changed_by='scheduler' "
                     "WHERE group_id=%s", (now, group_id))

    @staticmethod
    def _plan_groups(conn, plan):
        return conn.execute(
            "SELECT groups FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "AND revision=%s",
            (plan.player_id, plan.authority_epoch, plan.revision),
        ).fetchone()["groups"]

    def _offer(self, conn, plan: Plan, groups: dict[str, str]):
        # The offer is its variants' pin (decision 0034): each must be ready.
        self.media.require_ready_in(conn, (layer.variant for layer in plan.layers if layer.variant))
        conn.execute(
            "INSERT INTO plan_offers(player_id,authority_epoch,revision,plan_id,manifest,groups,"
            "created_at,valid_until,created_by,changed_at,changed_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'scheduler',%s,'scheduler')",
            (
                plan.player_id,
                plan.authority_epoch,
                plan.revision,
                plan.plan_id,
                Jsonb(plan.model_dump(mode="json")),
                Jsonb(_membership(plan.layers, groups)),
                plan.issued_at,
                plan.valid_until,
                plan.issued_at,
            ),
        )

    def advance(self) -> Advance:
        """One tick: the Runtime moves to now, then the wall is planned and offered, then due
        commits, pin expiry and cleanup settle. Each step is its own transaction and its own
        failure: planning that fails leaves the Runtime moved on and the commits settled."""
        failures: list[StepFailure] = []
        projection: Projection | None = None
        wakes: list[float | None] = []
        for step in ("runtime", "planning", "settle"):
            try:
                if step == "runtime":
                    wakes.append(self._advance_runtime())
                elif step == "planning":
                    projection = self._plan()
                    wakes.append(_renewal_at(projection, self.limits.renewal_seconds))
                else:
                    wakes.append(self._settle())
            except Exception as exc:
                failures.append(StepFailure(step=step, reason=scheduler_failure(exc)))
        return Advance(projection=projection, failures=tuple(failures),
                       wake_at=min((at for at in wakes if at is not None), default=None))

    def _advance_runtime(self) -> float | None:
        """The Runtime's current state moves to now (Program windows open and close, Runs end),
        saved alone, so no later step can hold it back. Answers the Runtime's next event."""
        with self._transaction() as conn, self.runtime.edit(
                conn, by="scheduler", command="advance") as runtime:
            # A scheduler waiting on either lock must advance at the committed
            # serialization cut, not at the time it first requested the cut.
            now = self.clock.utc()
            runtime.advance(now)
            return runtime.next_event_at(now)

    def _plan(self) -> Projection:
        """Pure future planning over the saved Runtime (read, never written here) and atomic
        offers. One Player whose authority went stale, or whose offer the media store refuses, is
        left out alone; the pass's issues are kept for the operator."""
        with self._transaction() as conn:
            runtime = self.runtime.read_locked(conn)
            now = self.clock.utc()
            configurations = self._configurations(conn, report=True, by="scheduler")
            offers = self._offers(conn, configurations)
            self.media.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            lines = self.media.scene_lines_in(conn)
            covers = self.skips.live_in(conn, now)
            holds = self.pauses.live_in(conn, now)
            never = self.never_show_list.listed_in(conn)
            authored = self.media.authored_in(conn, runtime.authored_asset_ids() | {
                cover.pinned for cover in covers if cover.pinned is not None})
            locks, refused = self._locks(conn, configurations, offers)
            for player, code in sorted(refused.items()):
                _left_out(player, code)
                del configurations[player]
                del offers[player]
            # Renewable extent changes only at a renewal boundary, avoiding heartbeat churn.
            quantum = self.limits.renewal_seconds
            horizon_end = (math.floor(now / quantum) + 1) * quantum + self.limits.horizon_seconds
            reserved, frame_uncertainty = self._held_in(conn, configurations, now)
            selections = runtime.planning_selections()
            history = self.shown.wall_history_in(
                conn, selections, now=now,
                window_seconds=86400 * max(
                    (selection.no_repeat_days for selection in selections.values()), default=0),
            )
            projection = project(
                runtime,
                now,
                bindings_by_player={
                    p: tuple(b for b in config.bindings if b.output_id in config.enabled_outputs)
                    for p, config in configurations.items()
                },
                lines=lines,
                authored_candidates=authored,
                locked_assignments=locks,
                history=history,
                settings=self.fit.fit_settings_in(conn),
                frame_outputs={output.frame_id: output for output in frame_outputs_in(conn)},
                horizon_seconds=horizon_end - now,
                plan_seconds=self.limits.plan_seconds,
                max_clock_uncertainty=self.limits.max_uncertainty,
                limits=PlannerLimits.over(self.limits.plan_seconds,
                                          self.limits.horizon_seconds + quantum),
                reserved=reserved,
                frame_uncertainty=frame_uncertainty,
                covers=covers,
                never=never,
                holds=holds,
            )
            groups = self._groups(conn, projection, now, horizon_end, configurations)
            for proposal in projection.players:
                self._propose(conn, proposal, configurations[proposal.player_id],
                              offers[proposal.player_id], groups, projection, now)
            # The planned photos with no variant yet are prepared: published with this cut.
            self.media.request_preparations_in(conn, projection.acquisitions)
            self.planning.record_in(conn, projection)
            return projection

    def _held_in(self, conn, configurations: dict[str, PlayerConfiguration], now: float,
                 ) -> tuple[list[Reservation], dict[str, float]]:
        """Decision 0032: what each Player may still be showing that this pass must keep off every
        other Frame, and each Frame's reported show-clock bound. From each Player's latest
        readiness in its current epoch: every stored offer at or past the revision it names
        (every offer when it has reported none) holds its continuations' photos
        (`held_reservations`), and each photo it reports carrying is reserved on its Frame from
        now on, while reported. A Player's Frames take its reported bound, grown by `DRIFT_RATE`
        for the time since Central received that report (a cut-off Pi's clock free-runs, and its
        report says nothing of the drift since; both times are Central's)."""
        reserved: list[Reservation] = []
        frame_uncertainty: dict[str, float] = {}
        for player, config in sorted(configurations.items()):
            frames = {binding.output_id: binding.frame_id for binding in config.bindings}
            row = conn.execute(
                "SELECT readiness, readiness_received_at AS received_at FROM player_show_state "
                "WHERE player_id=%s AND authority_epoch=%s AND readiness IS NOT NULL",
                (player, config.authority_epoch),
            ).fetchone()
            report = Readiness.model_validate(row["readiness"]) if row else None
            if report is not None:
                bound = report.clock_uncertainty + DRIFT_RATE * max(0.0, now - row["received_at"])
                frame_uncertainty.update({frame: bound for frame in frames.values()})
            for offer in conn.execute(
                "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                "AND revision>=%s ORDER BY revision",
                (player, config.authority_epoch, report.revision if report else 0),
            ).fetchall():
                try:
                    plan = Plan.model_validate(offer["manifest"])
                except ValidationError:
                    continue   # `_offers` names it; an offer that no longer reads holds nothing
                if plan.continuations:
                    reserved.extend(held_reservations(plan, guard=self._guard(
                        plan.bindings[0].frame_id, frame_uncertainty)))
            for carried in report.carried if report else ():
                frame = frames.get(carried.output_id)
                if frame is not None:
                    reserved.append(Reservation(frame_id=frame, asset_id=carried.asset_id,
                                                extent=Extent(start=now - self._guard(
                                                    frame, frame_uncertainty))))
        return reserved, frame_uncertainty

    def _guard(self, frame_id: str, frame_uncertainty: dict[str, float]) -> float:
        """The hand-over guard on `frame_id` (central/wall_plan/assignment.py's rule)."""
        return 2 * frame_clock_uncertainty(frame_id, self.limits.max_uncertainty,
                                           frame_uncertainty)

    def _propose(self, conn, proposal, config: PlayerConfiguration, offered: list[Plan],
                 groups: dict[str, str], projection: Projection, now: float) -> None:
        """Offer one Player its proposal. The Player is the unit of isolation: anything that
        fails while offering it rolls back to its own savepoint and is left out naming the code
        (`_left_out`; its latest offer stands), never the whole wall."""
        try:
            with conn.transaction():
                self._propose_one(conn, proposal, config, offered, groups, projection, now)
        except Exception as exc:   # noqa: BLE001 - the per-Player bulkhead
            _left_out(proposal.player_id, _refusal_code(exc))

    def _propose_one(self, conn, proposal, config: PlayerConfiguration, offered: list[Plan],
                     groups: dict[str, str], projection: Projection, now: float) -> None:
        """A new plan revision for one Player, unless it equals the latest, or a cut plan would
        withdraw a layer the latest offered (`_withdraws`)."""
        # Historical offers feed content locks only. Reconciliation,
        # revision reuse and backpressure belong to the fresh epoch.
        previous = [plan for plan in offered if plan.authority_epoch == config.authority_epoch]
        latest = previous[-1] if previous else None
        if latest and _withdraws(latest, proposal, projection, now):
            _left_out(proposal.player_id, "plan_shortened", failed=False)
            return
        bindings = config.bindings
        membership = _membership(proposal.layers, groups)
        if (
            latest
            and (latest.layers, latest.bindings, latest.unfilled, latest.continuations,
                 latest.valid_until)
            == (proposal.layers, bindings, proposal.unfilled, proposal.continuations,
                projection.valid_until)
            and self._plan_groups(conn, latest) == membership
        ):
            return
        if len(_unacknowledged(previous, _acknowledged_revision_in(
                conn, proposal.player_id, config.authority_epoch))) >= self.limits.max_offers:
            _left_out(proposal.player_id, "offer_backpressure", failed=False)
            return
        revision = conn.execute(
            "SELECT COALESCE(max(revision),0)+1 AS revision FROM plan_offers "
            "WHERE player_id=%s AND authority_epoch=%s",
            (proposal.player_id, config.authority_epoch),
        ).fetchone()["revision"]
        plan = Plan(
            plan_id=identity("plan-", [proposal.player_id, config.authority_epoch]),
            revision=revision,
            player_id=proposal.player_id,
            authority_epoch=config.authority_epoch,
            issued_at=now,
            valid_from=now,
            valid_until=projection.valid_until,
            bindings=bindings,
            layers=proposal.layers,
            unfilled=proposal.unfilled,
            continuations=proposal.continuations,
        )
        self._offer(conn, plan, groups)
        self._record_selected(conn, plan, projection)

    def _settle(self) -> float | None:
        """Commit the cues that are due and clean up expired offers and locks (their pins go with
        them). Answers the next due cue (`next_due_cue_in`)."""
        with self._transaction() as conn:
            now = self.clock.utc()
            configurations = self._configurations(conn, report=False, by="scheduler")
            self._commit_due(conn, configurations, now, by="scheduler")
            self._cleanup(
                conn,
                now,
                {
                    (configuration.player_id, configuration.authority_epoch)
                    for configuration in configurations.values()
                },
            )
            return next_due_cue_in(conn, now)

    def _record_selected(self, conn, plan: Plan, projection: Projection) -> None:
        """Roadmap 5a: record what each layer of an offered plan shows, for the layers whose
        selection names its photo, Scene and pass (a re-offered locked layer keeps its first
        row)."""
        selections = {selection.assignment_id: selection for selection in projection.selections}
        showings = []
        for layer in plan.layers:
            selection = selections.get(layer.assignment_id)
            if (selection is None or selection.asset_id is None or selection.scene_id is None
                    or selection.pass_number is None):
                continue
            showings.append(PhotoShowing(
                assignment_id=layer.assignment_id, asset_id=selection.asset_id,
                frame_id=layer.frame_id, scene_id=selection.scene_id,
                pass_number=selection.pass_number, start=layer.start, end=layer.end,
                fade_in=layer.fade_in, after_end=layer.after_end,
            ))
        self.shown.record_selected_in(conn, showings, by="scheduler")

    def _cleanup(self, conn, now, active_sessions: set[tuple[str, int]]):
        """Expiry releases leases, while one latest manifest preserves revision high-water.

        Every table pruned here is written only for what planning offered (an offer, its group,
        its commits and locks, keyed so a repeat is a no-op), so it cannot grow while this step
        fails. A Player's readiness outside its active session is cleared from its show state,
        once."""
        self.shown.prune_in(conn, now=now)
        conn.execute("DELETE FROM assignment_locks WHERE valid_until<=%s", (now,))
        conn.execute(
            "DELETE FROM execution_commits c USING plan_offers o WHERE "
            "(c.player_id,c.authority_epoch,c.revision)=(o.player_id,o.authority_epoch,o.revision) "
            "AND o.valid_until<=%s",
            (now,),
        )
        expired = conn.execute(
            "SELECT o.player_id,o.authority_epoch,o.revision,o.manifest,s.readiness "
            "FROM plan_offers o LEFT JOIN player_show_state s ON "
            "(s.player_id,s.authority_epoch)=(o.player_id,o.authority_epoch) "
            "WHERE o.valid_until<=%s",
            (now,),
        ).fetchall()
        for offer in expired:
            session = (offer["player_id"], offer["authority_epoch"])
            newer = conn.execute(
                "SELECT 1 FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                "AND revision>%s",
                (*session, offer["revision"]),
            ).fetchone()
            if session not in active_sessions or (newer and not _may_hold(offer, now)):
                conn.execute(
                    "DELETE FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                    "AND revision=%s",
                    (*session, offer["revision"]),
                )
        self._delete_superseded_in(conn, now, active_sessions)
        conn.execute(
            "DELETE FROM coordination_groups g WHERE valid_until<=%s "
            "AND NOT EXISTS(SELECT 1 FROM execution_commits c WHERE c.group_id=g.id)",
            (now,),
        )
        for feedback in conn.execute(
            "SELECT player_id,authority_epoch FROM player_show_state WHERE readiness IS NOT NULL"
        ).fetchall():
            session = (feedback["player_id"], feedback["authority_epoch"])
            if session not in active_sessions:
                conn.execute(
                    "UPDATE player_show_state SET readiness_sequence=NULL,readiness=NULL,"
                    "readiness_received_at=NULL,changed_at=%s,changed_by='scheduler' "
                    "WHERE player_id=%s AND authority_epoch=%s",
                    (now, *session),
                )

    @staticmethod
    def _delete_superseded_in(conn, now: float, active_sessions: set[tuple[str, int]]) -> None:
        """Every unexpired offer of an active session that the offer its Player acknowledged
        supersedes (`_superseded`) goes, with its commits: the Player has read past it and holds
        each of its live layers under the newer revision. One holding a commit of a cue the
        acknowledged revision holds none of (a cue that ended since) stays until it expires, so a
        cue's group is kept exactly as long as before (its commits keep it)."""
        for player_id, epoch in sorted(active_sessions):
            acknowledged = _acknowledged_revision_in(conn, player_id, epoch)
            rows = conn.execute(
                "SELECT revision,manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                "AND revision<=%s AND valid_until>%s ORDER BY revision",
                (player_id, epoch, acknowledged, now)).fetchall()
            if not rows or rows[-1]["revision"] != acknowledged:
                continue
            try:
                plans = [Plan.model_validate(row["manifest"]) for row in rows]
            except ValidationError:
                continue    # `_offers` names it; an offer that no longer reads is left alone
            groups: dict[int, set[str]] = {}
            for commit in conn.execute(
                    "SELECT revision,group_id FROM execution_commits WHERE player_id=%s "
                    "AND authority_epoch=%s AND revision<=%s", (player_id, epoch, acknowledged)):
                groups.setdefault(commit["revision"], set()).add(commit["group_id"])
            kept_groups = groups.get(acknowledged, set())
            gone = [plan.revision for plan in plans[:-1] if _superseded(plan, plans[-1], now)
                    and groups.get(plan.revision, set()) <= kept_groups]
            if gone:
                conn.execute("DELETE FROM execution_commits WHERE player_id=%s AND authority_epoch=%s "
                             "AND revision=ANY(%s)", (player_id, epoch, gone))
                conn.execute("DELETE FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                             "AND revision=ANY(%s)", (player_id, epoch, gone))

    @staticmethod
    def _observed_layer(plan: Plan, observation: Observation) -> tuple[Layer, bool] | None:
        """The plan layer an observation is about, and whether it named a continuation slot: the
        layer with its id, else (decision 0032) the source layer of the continuation slot that
        generated it (`continuation_source`, at its observed time); None when neither."""
        layer = next((a for a in plan.layers if a.assignment_id == observation.assignment_id), None)
        if layer is not None:
            return layer, False
        found = continuation_source(plan, observation.assignment_id, at=observation.observed_at)
        return None if found is None else (found[2], True)

    def _presented_from_an_earlier_offer(self, conn, player_id: str, observation: Observation,
                                         config) -> bool:
        """Roadmap 5a: a "presented" report stamped with a revision this Player was offered
        before the latest still counts as shown (a new revision is offered every cycle, so a
        report often races the next offer; losing it would let the photo come back sooner); a
        continuation slot's counts for its source photo (decision 0032: a Frame that played its
        continuation through an outage reports under the revision it held).
        Only the shown history is written, and the report is still refused as stale."""
        if observation.status != "presented":
            return False
        # Its own revision, else (cleanup deletes an offer its Player acknowledged past,
        # `_delete_superseded_in`) the first retained later one that holds the same layer.
        observed = None
        for row in conn.execute(
            "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "AND revision>=%s ORDER BY revision",
            (player_id, observation.authority_epoch, observation.revision),
        ).fetchall():
            offered = Plan.model_validate(row["manifest"])
            if offered.plan_id != observation.plan_id:
                return False
            observed = self._observed_layer(offered, observation)
            if observed is not None or offered.revision == observation.revision:
                break
        if observed is None or not self._authorized(observed[0], config):
            return False
        self.shown.record_presented_in(conn, observed[0].assignment_id, self.clock.utc(), by="node")
        return True

    def _current_plan(self, conn, player_id, epoch) -> Plan | None:
        row = conn.execute(
            "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "ORDER BY revision DESC LIMIT 1",
            (player_id, epoch),
        ).fetchone()
        return Plan.model_validate(row["manifest"]) if row else None

    def delivery(self, player_id: str, epoch: int, *, finalize=None) -> dict:
        """Read delivery and optionally finalize its wire challenge under one lock cut. A read: an
        unchanged state takes no row lock and writes nothing (`_configuration`'s `lock=False`,
        Registry's issue of the same delivery), so it notifies no socket."""
        with self._transaction() as conn:
            config = self._configuration(conn, player_id, epoch, lock=False)
            now = self.clock.utc()
            identify = conn.execute(
                "SELECT s.identify,p.authority_epoch AS current_epoch,p.retired_at,o.observation,"
                "EXISTS(SELECT 1 FROM frames f WHERE f.player_id=s.player_id "
                "AND f.output_id=s.identify->>'output_id') AS is_bound "
                "FROM player_show_state s JOIN players p ON p.id=s.player_id "
                "LEFT JOIN outputs o ON o.player_id=s.player_id "
                "AND o.output_id=s.identify->>'output_id' "
                "WHERE s.player_id=%s AND s.identify IS NOT NULL",
                (player_id,),
            ).fetchone()
            identify_output = None
            if identify:
                request = identify["identify"]
                remaining = request["expires_at"] - now
                connected = bool(identify["observation"] and
                                 identify["observation"].get("connected", False))
                if (identify["retired_at"] is None and identify["current_epoch"] == epoch
                        and request["authority_epoch"] == epoch and remaining > 0
                        and connected and not identify["is_bound"]):
                    identify_output = IdentifyOutput(
                        request_id=request["request_id"],
                        output_id=request["output_id"],
                        authority_epoch=request["authority_epoch"],
                        remaining_seconds=min(15.0, remaining),
                    )
                else:
                    # Spent: cleared once, by the request id it named.
                    conn.execute(
                        "UPDATE player_show_state SET identify=NULL,changed_at=%s,"
                        "changed_by='central' WHERE player_id=%s "
                        "AND identify->>'request_id'=%s",
                        (now, player_id, request["request_id"]),
                    )
            plan = self._current_plan(conn, player_id, epoch)
            if plan and (
                plan.valid_until <= self.clock.utc()
                or plan.bindings != config.bindings
                or any(not self._authorized(layer, config) for layer in plan.layers)
            ):
                plan = None
            commits, revocations = (), ()
            if plan:
                membership = self._plan_groups(conn, plan)
                skipped = conn.execute(
                    "SELECT id,skip_sequence FROM coordination_groups "
                    "WHERE status='skipped' AND skip_sequence IS NOT NULL"
                ).fetchall()
                revocations = tuple(
                    Revocation(
                        plan_id=plan.plan_id,
                        revision=plan.revision,
                        authority_epoch=epoch,
                        sequence=g["skip_sequence"],
                        mode="invalidate",
                        assignment_ids=tuple(
                            sorted(a for a, group in membership.items() if group == g["id"])
                        ),
                    )
                    for g in sorted(skipped, key=lambda g: g["skip_sequence"])
                    if g["id"] in membership.values()
                )
                rows = conn.execute(
                    "SELECT group_id,assignment_id,committed_at,readiness_sequence "
                    "FROM execution_commits WHERE player_id=%s AND authority_epoch=%s "
                    "AND revision=%s AND valid",
                    (player_id, epoch, plan.revision),
                ).fetchall()
                # One Commit per grant (group, readiness sequence): a grant written later at the
                # same sequence (another group, or a re-granted member) never changes a Commit
                # the Player already accepted.
                grants: dict[tuple[int, str], list] = {}
                for r in rows:
                    grants.setdefault((r["readiness_sequence"], r["group_id"]), []).append(r)
                commits = tuple(
                    Commit(
                        plan_id=plan.plan_id,
                        revision=plan.revision,
                        authority_epoch=epoch,
                        readiness_sequence=sequence,
                        assignment_ids=tuple(sorted(r["assignment_id"] for r in members)),
                        committed_at=max(r["committed_at"] for r in members),
                    )
                    for (sequence, _), members in sorted(grants.items())
                )
            delivery = {
                "configuration": config,
                "plan": plan,
                "commits": commits,
                "revocations": revocations,
                "identify_output": identify_output,
            }
            return finalize(conn, delivery) if finalize is not None else delivery

    @staticmethod
    def output_interruptions_in(conn) -> tuple[OutputInterruption, ...]:
        """Unresolved Output losses that fence a current Binding, from a caller-owned transaction.

        A loss is keyed by (player, authority epoch, Output, Frame, binding generation) and fences
        only on that exact key (`_commit_due`). A row from an earlier epoch, an earlier binding
        generation or another Frame on the same Output is therefore not served: a reader can
        never attach it to whatever Frame is bound there now. Read-only; takes no lock."""
        rows = conn.execute(
            "SELECT l.frame_id,l.player_id,l.output_id,l.binding_generation,"
            "l.cause_layer,l.interrupted_at FROM output_losses l "
            "JOIN players p ON p.id=l.player_id AND p.authority_epoch=l.authority_epoch "
            "JOIN frames f ON f.id=l.frame_id AND f.generation=l.binding_generation "
            "AND f.player_id=l.player_id AND f.output_id=l.output_id "
            "WHERE l.resolved_at IS NULL ORDER BY l.frame_id"
        ).fetchall()
        return tuple(OutputInterruption.model_validate(dict(row)) for row in rows)

    @staticmethod
    def frame_plans_in(conn, now: float) -> dict[str, FramePlan]:
        """Frame id -> what each Player's newest plan offer, still valid at `now`, holds for it,
        from a caller-owned transaction: its unfilled spans ending after `now`, and of its layers
        ending after `now` how many there are and how many the Player's readiness for that same
        offer (plan and revision) names prepared or failed (no such readiness: none). A Frame the
        offer has neither a layer nor a span for is absent. Read-only; takes no lock."""
        rows = conn.execute(
            "SELECT o.manifest, s.readiness FROM players p "
            "JOIN LATERAL (SELECT manifest,valid_until FROM plan_offers "
            "WHERE player_id=p.id AND authority_epoch=p.authority_epoch "
            "ORDER BY revision DESC LIMIT 1) o ON TRUE "
            "LEFT JOIN player_show_state s ON s.player_id=p.id AND s.authority_epoch=p.authority_epoch "
            "WHERE p.retired_at IS NULL AND o.valid_until > %s", (now,)).fetchall()
        plans: dict[str, FramePlan] = {}
        for row in rows:
            plan = Plan.model_validate(row["manifest"])
            report = None if row["readiness"] is None else Readiness.model_validate(row["readiness"])
            if report is not None and (report.plan_id, report.revision) != (plan.plan_id, plan.revision):
                report = None
            prepared = frozenset(()) if report is None else frozenset(report.prepared)
            failed = frozenset(()) if report is None else frozenset(each.assignment_id for each in report.failures)
            frames = {binding.output_id: binding.frame_id for binding in plan.bindings}
            for frame_id in frames.values():
                layers = [layer for layer in plan.layers if layer.frame_id == frame_id and layer.drawn_end > now
                          and layer.presentation == "media"]
                spans = tuple(span for span in plan.unfilled
                              if frames.get(span.output_id) == frame_id and span.end > now)
                if layers or spans:
                    plans[frame_id] = FramePlan(
                        unfilled=spans, layers=len(layers),
                        prepared=sum(layer.assignment_id in prepared for layer in layers),
                        failed=sum(layer.assignment_id in failed for layer in layers))
        return plans

    # --- The everyday actions (decision 0036, slice 3) --------------------------------------------

    def skip(self, scope: SkipScope, target_id: str, kind: SkipKind, request_id: str, *, by: Actor,
             facts: FrameFactsReader) -> SkipAnswer:
        """Skip or Previous on a wall (every Frame of it, the owner's S1) or one Frame, under the
        Coordination lock: each Frame that can move (central/everyday/policy.py `stays`, judged
        on the same facts as Home's `can`) gets a cover from one shared instant, each that cannot
        stays and is named. One row in `skips`; its commit wakes the scheduler and the Players, so
        the next planning pass offers the covers. A repeated `request_id` answers the stored row
        (`duplicate`) and changes nothing. CoordinationError "unknown_wall"/"unknown_frame" (404);
        RegistryError "nothing-moved" (409, with `stayed`) when no Frame could move."""
        with self._transaction() as conn:
            stored = self.skips.get_in(conn, request_id)
            if stored is not None:
                return _skip_answer(stored, duplicate=True)
            frame_ids = self._skip_frames_in(conn, scope, target_id)
            now = self.clock.utc()
            at = the_shared_instant(now)
            found = facts(conn, now, frame_ids)
            cause = CoverCause.NEXT if kind is SkipKind.NEXT else CoverCause.PREVIOUS
            cue = identity("skip-", [request_id]) if scope is SkipScope.WALL else None
            covers, stayed = [], []
            for frame_id in frame_ids:
                frame = found.get(frame_id)
                usable = self._usable_in(conn, frame) if frame is not None and kind is SkipKind.PREVIOUS else None
                reason = StayReason.NOT_SET_UP if frame is None else stays(frame, kind, at, usable=usable, now=now)
                if reason is not None:
                    stayed.append(Stayed(frame_id=frame_id, reason=reason))
                    continue
                covers.append(cover_for(frame, kind, cause, request_id=request_id, at=at, now=now,
                                        cue=cue, usable=usable))
            if not covers:
                raise RegistryError(EverydayRefusal.NOTHING_MOVED.value, 409, details={
                    "stayed": [each.model_dump(mode="json") for each in stayed]})
            record = SkipRecord(request_id=request_id, scope=scope, target_id=target_id, cause=cause.value,
                                outcome=SkipOutcome(at=at, covers=tuple(covers), stayed=tuple(stayed)), by=by)
            self.skips.add_in(conn, record, now=now)
            return _skip_answer(record, duplicate=False)

    def never_show(self, request_id: str, asset_id: str, frame_id: str | None, *, by: Actor,
                   facts: FrameFactsReader) -> NeverShowAnswer:
        """Put a photo on the Never show list, under the Coordination lock. Where a Frame shows it
        at the shared instant and can move, it is skipped there (a cover, cause never-show); a
        Frame that cannot move shows it to its end, once. A showing already offered to another
        Frame (downloaded or downloading for that slot) is a lock and plays that one last time
        (the owner's S5): the answer names each in `last_showings`. From a Frame's menu,
        `frame_id` is the Frame it was seen on: RegistryError "changed" (409) when that Frame
        shows another photo now. A repeated `request_id` answers what it did (`duplicate`)."""
        with self._transaction() as conn:
            # A repeat is known by either row it wrote: the list's (when it listed the photo) or
            # its skips row (when the photo was listed already, by another request).
            stored = self.skips.get_in(conn, request_id)
            if stored is not None or self.never_show_list.requested_in(conn, request_id) is not None:
                outcome = stored.outcome if stored is not None else SkipOutcome(at=self.clock.utc())
                return NeverShowAnswer(asset_id=asset_id, duplicate=True,
                                       skipped_on=tuple(cover.frame_id for cover in outcome.covers),
                                       stayed=outcome.stayed)
            now = self.clock.utc()
            at = the_shared_instant(now)
            found = facts(conn, now, None)
            if frame_id is not None:
                if frame_id not in found:
                    raise CoordinationError(EverydayRefusal.UNKNOWN_FRAME.value, 404)
                if photo_of(showing_at(found[frame_id].layers, now)) != asset_id:
                    raise RegistryError(EverydayRefusal.CHANGED.value, 409)
            self.never_show_list.add_in(conn, request_id, asset_id, now=now, by=by)
            covers, stayed = [], []
            held = {pause.frame_id: pause for pause in self.pauses.live_in(conn, now)
                    if photo_of(pause.held) == asset_id}
            frames = dict(sorted(found.items()))
            for each_id, frame in frames.items():
                if each_id in held:
                    # A paused Frame holding it: its pause ends there (Carry on), and what its
                    # hold covered is skipped as any Frame showing it.
                    self._carry_on_in(conn, held[each_id], now=now, by=by)
                    frame = frames[each_id] = replace(frame, paused=False, layers=tuple(
                        layer for layer in frame.layers if layer.assignment_id != held[each_id].held.assignment_id))
                if photo_of(showing_at(frame.layers, at)) != asset_id:
                    continue
                listed = replace(frame, listed=frame.listed | {asset_id})
                reason = stays(listed, SkipKind.NEXT, at, now=now)
                if reason is not None:
                    stayed.append(Stayed(frame_id=each_id, reason=reason))
                    continue
                covers.append(cover_for(listed, SkipKind.NEXT, CoverCause.NEVER_SHOW, request_id=request_id,
                                        at=at, now=now, cue=None))
            # Its last showings: each regular layer of it the Frame will still draw from the shared
            # instant, under its covers, this request's among them (`draws`, `showing_at`'s rule);
            # a Frame that stays is named as one that stayed.
            stays_put = {stay.frame_id for stay in stayed}
            last = []
            for each_id, frame in frames.items():
                if each_id in stays_put:
                    continue
                known = {layer.assignment_id for layer in frame.layers}
                drawn = (*frame.layers, *(cover_drawn(cover) for cover in (*frame.recent, *covers)
                                          if cover.frame_id == each_id and cover.cover_id not in known))
                last.extend(LastShowing(frame_id=layer.frame_id, start=layer.start, until=layer.drawn_end)
                            for layer in frame.layers
                            if photo_of(layer) == asset_id and not layer.action and draws(drawn, layer, at))
            if covers or stayed:
                self.skips.add_in(conn, SkipRecord(
                    request_id=request_id, scope=SkipScope.PHOTO, target_id=asset_id,
                    cause=CoverCause.NEVER_SHOW.value,
                    outcome=SkipOutcome(at=at, covers=tuple(covers), stayed=tuple(stayed)), by=by), now=now)
            return NeverShowAnswer(asset_id=asset_id, skipped_on=tuple(cover.frame_id for cover in covers),
                                   stayed=tuple(stayed), last_showings=tuple(last))

    def pause(self, request_id: str, frame_ids: tuple[str, ...], seconds: float, *, by: Actor,
              facts: FrameFactsReader) -> PauseAnswer:
        """Pause: each Frame holds the photo it shows, under the Coordination lock (decision 0036).
        Each Frame that may be paused (central/everyday/policy.py `pause_refusal`, judged on the
        facts Home's `can` reads) gets a hold from the shared instant to its end, a layer above
        every Run (`PAUSE_RANK`); each that may not stays and is named. One row per Frame in
        `frame_pauses`; the commit wakes the scheduler and the Players. A repeated `request_id`
        answers the Frames it paused (`duplicate`) and changes nothing. CoordinationError
        "unknown_frame" (404); RegistryError "nothing-paused" (409, with `stayed`)."""
        with self._transaction() as conn:
            stored = self.pauses.of_in(conn, request_id)
            if stored:
                return PauseAnswer(pause_id=request_id, ends_at=stored[0].ends_at, duplicate=True,
                                   paused=tuple(PausedFrame(frame_id=pause.frame_id) for pause in stored))
            for frame_id in frame_ids:
                if conn.execute("SELECT 1 FROM frames WHERE id=%s", (frame_id,)).fetchone() is None:
                    raise CoordinationError(EverydayRefusal.UNKNOWN_FRAME.value, 404)
            now = self.clock.utc()
            at = the_shared_instant(now)
            ends_at = at + seconds
            found = facts(conn, now, list(frame_ids))
            paused, stayed = [], []
            for frame_id in sorted(set(frame_ids)):
                frame = found.get(frame_id)
                reason = (PauseRefusal.NOT_SET_UP if frame is None
                          else pause_refusal(frame, at, seen_at=now))
                if reason is not None:
                    stayed.append(PauseStayed(frame_id=frame_id, reason=reason))
                    continue
                held = hold_for(frame, pause_id=request_id, at=at, ends_at=ends_at, priority=PAUSE_RANK,
                                seen_at=now)
                paused.append(Pause(pause_id=request_id, frame_id=frame_id, by=by, started_at=at,
                                    ends_at=ends_at, scene_id=_scene_of(frame, showing_at(frame.layers, now)),
                                    held=held))
            if not paused:
                raise RegistryError(EverydayRefusal.NOTHING_PAUSED.value, 409, details={
                    "stayed": [each.model_dump(mode="json") for each in stayed]})
            for pause in paused:
                self.pauses.put_in(conn, pause, now=now)
            return PauseAnswer(pause_id=request_id, ends_at=ends_at,
                               paused=tuple(PausedFrame(frame_id=pause.frame_id) for pause in paused),
                               stayed=tuple(stayed))

    def carry_on(self, pause_id: str, frame_id: str | None = None, *, by: Actor) -> CarryOnAnswer:
        """Carry on: end the pause `pause_id` on every Frame it holds (or `frame_id` alone) at once,
        under the Coordination lock: each hold is cut `END_LEAD` and its fade-out from now, so the
        schedule beneath, which kept advancing, shows at its current state with the usual fade
        (the owner's S3). CoordinationError "unknown_pause" (404) when it holds no such Frame now;
        a repeat once it ended answers the same Frames with nothing changed."""
        with self._transaction() as conn:
            now = self.clock.utc()
            rows = [pause for pause in self.pauses.of_in(conn, pause_id)
                    if frame_id is None or pause.frame_id == frame_id]
            if not rows:
                raise CoordinationError(EverydayRefusal.UNKNOWN_PAUSE.value, 404)
            ended = []
            for pause in rows:
                if pause.ended_at is None and pause.live(now):
                    self._carry_on_in(conn, pause, now=now, by=by)
                ended.append(PausedFrame(frame_id=pause.frame_id))
            current = self.pauses.of_in(conn, pause_id)
            until = max((pause.held.drawn_end for pause in current
                         if frame_id is None or pause.frame_id == frame_id), default=now)
            return CarryOnAnswer(pause_id=pause_id, ended=tuple(ended), until=max(now, until))

    def _carry_on_in(self, conn, pause: Pause, *, now: float, by: Actor) -> None:
        held = carried_on(pause.held, now)
        self.pauses.end_in(conn, pause, pause.held if held is None else held, now=now, by=by)

    def show_again(self, asset_id: str) -> bool:
        """Undo and Show again: take the photo off the Never show list (the next planning pass may
        place it); False when it was not on it."""
        with self._transaction() as conn:
            return self.never_show_list.remove_in(conn, asset_id)

    def never_show_entries(self) -> NeverShowListView:
        """Scenes › Never show list."""
        with self.db.transaction() as conn:
            return NeverShowListView(photos=self.never_show_list.entries_in(conn))

    @staticmethod
    def _skip_frames_in(conn, scope: SkipScope, target_id: str) -> list[str]:
        if scope is SkipScope.FRAME:
            if conn.execute("SELECT 1 FROM frames WHERE id=%s", (target_id,)).fetchone() is None:
                raise CoordinationError(EverydayRefusal.UNKNOWN_FRAME.value, 404)
            return [target_id]
        if conn.execute("SELECT 1 FROM walls WHERE surface_id=%s", (target_id,)).fetchone() is None:
            raise CoordinationError(EverydayRefusal.UNKNOWN_WALL.value, 404)
        return [row["id"] for row in conn.execute(
            "SELECT id FROM frames WHERE surface_id=%s ORDER BY id", (target_id,)).fetchall()]

    def _usable_in(self, conn, frame: FrameFacts) -> Callable[[str], bool]:
        """Previous's test of a photo in the Frame's history: still in the library and still
        fitting the Frame at the house's crop limit (the photo fit rule's size-only part, as a
        hand-placed photo is judged)."""
        output = next((each for each in frame_outputs_in(conn) if each.frame_id == frame.frame_id), None)
        target = None if output is None else fit_target(output)
        candidates = self.media.authored_in(conn, frozenset(entry.asset_id for entry in frame.history))
        settings = self.fit.fit_settings_in(conn)
        crop_limit = crop_limit_of(SceneSelection(), settings)
        return lambda asset_id: (target is not None and asset_id in candidates
                                 and eligible(candidates[asset_id], target, crop_limit, settings))

    @staticmethod
    def frame_layers_in(conn, now: float) -> dict[str, tuple[Layer, ...]]:
        """Frame id -> the layers each Player's newest plan offer, still valid at `now`, holds for
        it that are not over at `now` (decision 0036: what the everyday actions read), from a
        caller-owned transaction. Read-only; takes no lock."""
        rows = conn.execute(
            "SELECT o.manifest FROM players p "
            "JOIN LATERAL (SELECT manifest,valid_until FROM plan_offers "
            "WHERE player_id=p.id AND authority_epoch=p.authority_epoch "
            "ORDER BY revision DESC LIMIT 1) o ON TRUE "
            "WHERE p.retired_at IS NULL AND o.valid_until > %s", (now,)).fetchall()
        found: dict[str, list[Layer]] = {}
        for row in rows:
            try:
                plan = Plan.model_validate(row["manifest"])
            except ValidationError:
                continue    # `_offers` names it; an offer that no longer reads holds nothing here
            frames = {binding.output_id: binding.frame_id for binding in plan.bindings}
            for layer in plan.layers:
                if layer.drawn_end > now and frames.get(layer.output_id) == layer.frame_id:
                    found.setdefault(layer.frame_id, []).append(layer)
        return {frame_id: tuple(layers) for frame_id, layers in found.items()}

    def readiness(self, player_id: str, report: Readiness) -> bool:
        """Accept the Player's readiness; False for one already held (its number at or below the
        one written: the Player numbers it once per change and sends it again only when an answer
        was lost), which is answered before anything is read under a lock or written."""
        now = self.clock.utc()
        with self._transaction() as conn:
            if not self.installation.session_is_current_in(conn, player_id, report.authority_epoch):
                raise CoordinationError("stale_authority", 403)
            if (held := self._readiness_sequence_in(conn, player_id, report.authority_epoch)
                    ) is not None and report.sequence <= held:
                return False
            configurations = self._configurations(conn, report=False, by="central")
            config = configurations.get(player_id)
            if config is None or config.authority_epoch != report.authority_epoch:
                raise CoordinationError("stale_authority", 403)
            row = conn.execute(
                "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                "AND revision=%s AND plan_id=%s AND valid_until>%s",
                (player_id, report.authority_epoch, report.revision, report.plan_id, now),
            ).fetchone()
            if row is None:
                raise CoordinationError("unknown_offer")
            plan = Plan.model_validate(row["manifest"])
            layers = {layer.assignment_id: layer for layer in plan.layers}
            reported = (
                set(report.secured)
                | set(report.prepared)
                | {f.assignment_id for f in report.failures}
            )
            if not reported <= layers.keys():
                raise CoordinationError("unknown_assignment")
            if any(not self._authorized(layers[a], config) for a in reported):
                raise CoordinationError("stale_binding", 403)
            variants = []
            for assignment in report.secured:
                layer = layers[assignment]
                if layer.end <= now:
                    continue
                old_lock = conn.execute(
                    "SELECT layer FROM assignment_locks WHERE player_id=%s "
                    "AND authority_epoch=%s AND assignment_id=%s",
                    (player_id, report.authority_epoch, assignment),
                ).fetchone()
                if old_lock and not same_execution(Layer.model_validate(old_lock["layer"]), layer):
                    raise CoordinationError("content_lock_conflict")
                conn.execute(
                    "INSERT INTO assignment_locks(player_id,authority_epoch,assignment_id,layer,"
                    "secured_at,valid_until,created_at,created_by,changed_at,changed_by) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,'central',%s,'central') "
                    "ON CONFLICT(player_id,authority_epoch,assignment_id) DO NOTHING",
                    (
                        player_id,
                        report.authority_epoch,
                        assignment,
                        Jsonb(layer.model_dump(mode="json")),
                        now,
                        layer.end,
                        now,
                        now,
                    ),
                )
                if layer.variant:
                    variants.append(layer.variant)
            # The lock is its variant's pin (decision 0034): each must be ready.
            self.media.require_ready_in(conn, variants)
            # The show state's row is this epoch's: `_configurations` just kept its configuration.
            conn.execute(
                "UPDATE player_show_state SET readiness_sequence=%s,readiness_received_at=%s,"
                "readiness=%s,changed_at=%s,changed_by='node' "
                "WHERE player_id=%s AND authority_epoch=%s",
                (
                    report.sequence,
                    now,
                    Jsonb(report.model_dump(mode="json")),
                    now,
                    player_id,
                    report.authority_epoch,
                ),
            )
            # Loss is local first on the Player; this persists central invalidation.
            current = self._current_plan(conn, player_id, report.authority_epoch)
            if (
                current
                and current.revision == report.revision
                and current.bindings == config.bindings
            ):
                for layer in current.layers:
                    if layer.assignment_id not in report.prepared or not report.capacity_ok:
                        conn.execute(
                            "UPDATE execution_commits SET valid=FALSE,changed_at=%s,"
                            "changed_by='central' WHERE player_id=%s "
                            "AND authority_epoch=%s AND assignment_id=%s AND valid",
                            (now, player_id, report.authority_epoch, layer.assignment_id),
                        )
                # A grant the Player saw and no longer holds: only the one Central still holds
                # (same sequence) is invalidated, for `_commit_due` to re-grant once; a lost
                # grant at an older sequence is already re-granted.
                for lost in report.lost_grants:
                    conn.execute(
                        "UPDATE execution_commits SET valid=FALSE,changed_at=%s,"
                        "changed_by='central' WHERE player_id=%s "
                        "AND authority_epoch=%s AND revision=%s AND assignment_id=%s "
                        "AND readiness_sequence=%s AND valid",
                        (now, player_id, report.authority_epoch, report.revision,
                         lost.assignment_id, lost.readiness_sequence),
                    )
            self._commit_due(conn, configurations, now, by="central")
            return True

    @staticmethod
    def _readiness_sequence_in(conn, player_id: str, epoch: int) -> int | None:
        """The number of the Player's readiness held for its epoch, or None."""
        row = conn.execute(
            "SELECT readiness_sequence FROM player_show_state WHERE player_id=%s "
            "AND authority_epoch=%s",
            (player_id, epoch),
        ).fetchone()
        return None if row is None else row["readiness_sequence"]

    @staticmethod
    def _observed_sequence_in(conn, player_id: str, epoch: int) -> int | None:
        """The highest observation report number held for the Player's epoch; a report at or below
        it is a repeat (answered, nothing written)."""
        row = conn.execute(
            "SELECT observed_sequence FROM player_show_state WHERE player_id=%s "
            "AND authority_epoch=%s",
            (player_id, epoch),
        ).fetchone()
        return None if row is None else row["observed_sequence"]

    def _commit_due(self, conn, configurations, now, *, by: Actor):
        interrupted = {(row["player_id"], row["authority_epoch"], row["output_id"], row["frame_id"], row["binding_generation"])
                       for row in conn.execute("SELECT player_id,authority_epoch,output_id,frame_id,binding_generation "
                                               "FROM output_losses WHERE resolved_at IS NULL").fetchall()}
        plans = {
            p: self._current_plan(conn, p, c.authority_epoch) for p, c in configurations.items()
        }
        plan_groups = {p: self._plan_groups(conn, plan) for p, plan in plans.items() if plan}
        feedback = {}
        for row in conn.execute("SELECT player_id,authority_epoch,readiness FROM player_show_state "
                                "WHERE readiness IS NOT NULL").fetchall():
            config = configurations.get(row["player_id"])
            if config and config.authority_epoch == row["authority_epoch"]:
                feedback[row["player_id"]] = (row, Readiness.model_validate(row["readiness"]))
        for group in conn.execute(
            "SELECT * FROM coordination_groups WHERE status!='skipped' "
            "AND valid_until>%s AND starts_at<=%s ORDER BY starts_at,id",
            (now, now + PREPARE_SECONDS),
        ).fetchall():
            members, participants = [], {}
            for assignment, owner in group["members"].items():
                participant = self._participant(
                    assignment, owner, group["id"], plans, plan_groups, feedback, configurations,
                    interrupted, now,
                )
                if participant is not None:
                    participants[assignment] = participant
                members.append(CueReadiness(
                    assignment_id=assignment,
                    ended=group["member_ends"].get(assignment, group["valid_until"]) <= now,
                    ready=participant is not None,
                ))
            granted = grantable(members, whole=starts_whole(group["status"], now, group["deadline"]))
            if not granted:
                continue
            # Only a missing or invalidated grant is written: a grant the Player holds is
            # never rewritten, so its Commit and the control delivery stay unchanged.
            held = {(r["player_id"], r["authority_epoch"], r["revision"], r["assignment_id"])
                    for r in conn.execute(
                        "SELECT player_id,authority_epoch,revision,assignment_id "
                        "FROM execution_commits WHERE group_id=%s AND valid", (group["id"],))}
            for assignment in granted:
                plan, layer, sequence = participants[assignment]
                if (plan.player_id, plan.authority_epoch, plan.revision,
                        layer.assignment_id) in held:
                    continue
                conn.execute(
                    "INSERT INTO execution_commits(player_id,authority_epoch,revision,"
                    "assignment_id,group_id,committed_at,valid,readiness_sequence,created_at,"
                    "created_by,changed_at,changed_by) VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(player_id,authority_epoch,revision,assignment_id) "
                    "DO UPDATE SET valid=TRUE,group_id=EXCLUDED.group_id,"
                    "committed_at=EXCLUDED.committed_at,"
                    "readiness_sequence=EXCLUDED.readiness_sequence,"
                    "changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by",
                    (
                        plan.player_id,
                        plan.authority_epoch,
                        plan.revision,
                        layer.assignment_id,
                        group["id"],
                        now,
                        sequence,
                        now,
                        by,
                        now,
                        by,
                    ),
                )
            if group["status"] != "committed":
                conn.execute(
                    "UPDATE coordination_groups SET status='committed',changed_at=%s,changed_by=%s "
                    "WHERE id=%s",
                    (now, by, group["id"]),
                )

    def _participant(self, assignment, owner, group_id, plans, plan_groups, feedback,
                     configurations, interrupted, now) -> tuple[Plan, Layer, int] | None:
        """What a grant to this cue member is written from, or None while its Player does not
        report it prepared on this cue's plan. Freshness is the readiness sequence the Commit
        echoes, which the Player checks (decision 0032): never the report's age by Central's
        receipt, nor the Player's clock bound."""
        if owner is None:
            return None
        player, epoch, output, generation = owner
        plan, state = plans.get(player), feedback.get(player)
        layer = next((a for a in plan.layers if a.assignment_id == assignment), None) if plan else None
        if (
            not plan
            or not state
            or not layer
            or (player, epoch, output, layer.frame_id, generation) in interrupted
            or not self._authorized(layer, configurations[player])
            or plan.bindings != configurations[player].bindings
            or plan.valid_until <= now
            or plan_groups[player].get(assignment) != group_id
        ):
            return None
        _, report = state
        if (
            plan.authority_epoch != epoch
            or plan.revision != report.revision
            or plan.plan_id != report.plan_id
            or assignment not in report.prepared
            or not report.capacity_ok
            or layer.output_id != output
            or layer.binding_generation != generation
        ):
            return None
        return plan, layer, report.sequence

    def observe(self, player_id: str, observation: Observation):
        if not self._observe(player_id, observation):
            # Refused after its transaction committed, so a stale "presented" report's shown
            # row stays written while the Player still hears stale_observation (its cue to
            # fetch the newer plan).
            raise CoordinationError("stale_observation")

    def _observe(self, player_id: str, observation: Observation) -> bool:
        """Take one observation once: the Player numbers its reports once per change, in order,
        within its epoch (player/executor.py `_report`), so one at or below the show state's mark
        (`_observed_sequence_in`) is a repeat, answered alike and writing nothing. A new one moves
        the mark and, "presented", is shown history. False when it names a plan revision other
        than the latest (a stale "presented" one still counts as shown,
        `_presented_from_an_earlier_offer`)."""
        with self._transaction() as conn:
            config = self._configuration(conn, player_id, observation.authority_epoch)
            plan = self._current_plan(conn, player_id, observation.authority_epoch)
            if not plan or (plan.plan_id, plan.revision) != (
                observation.plan_id,
                observation.revision,
            ):
                self._presented_from_an_earlier_offer(conn, player_id, observation, config)
                return False
            observed = self._observed_layer(plan, observation)
            if observed is None or not self._authorized(observed[0], config):
                raise CoordinationError("stale_binding", 403)
            layer, _ = observed
            held = self._observed_sequence_in(conn, player_id, observation.authority_epoch)
            if held is not None and observation.sequence <= held:
                return True     # taken when it was first sent: Central answered it late
            now = self.clock.utc()
            # The show state's row is this epoch's: `_configuration` just kept its configuration.
            conn.execute(
                "UPDATE player_show_state SET observed_sequence=%s,changed_at=%s,changed_by='node' "
                "WHERE player_id=%s AND authority_epoch=%s",
                (observation.sequence, now, player_id, observation.authority_epoch),
            )
            if observation.status == "presented":
                # Roadmap 5a: "Shown" means it was displayed on a frame (owner, 2026-10-11); a
                # continuation slot counts for its source photo (decision 0032).
                self.shown.record_presented_in(conn, layer.assignment_id, now, by="node")
        return True
