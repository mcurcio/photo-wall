"""Persisted offers, byte locks and revocable group execution authority.

No upstream transport, conversion or renderer work occurs in these transactions.
See docs/module-coordination.md for lock order and conservative offer leases.
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass

from psycopg.types.json import Jsonb
from pydantic import Field

from central.db import Database, DatabaseTransactionClock
from central.equipment_drain import fenced_players_in
from central.execution_outcomes import ExecutionOutcome, ExecutionOutcomeRouter
from central.installation_models import OutputInterruption, PlayerReports
from central.installation_ports import InstallationSessions
from central.installation_repository import PostgresInstallationRepository
from central.media_ports import CoordinationMedia, MediaPin
from central.media_repository import MediaRepository
from central.planner import (
    PlannerLimits,
    Projection,
    assignment_id,
    eligible,
    project,
    same_execution,
)
from central.planner import (
    handle_execution_outcome as handle_planning_outcome,
)
from central.registry import RegistryError
from central.runtime import Scene
from central.runtime import handle_execution_outcome as handle_runtime_outcome
from central.runtime_store import RuntimeStore
from central.transaction_locks import COORDINATION_LOCK, holds_runtime_locks_in
from contracts.models import (
    Commit,
    IdentifyOutput,
    Layer,
    Model,
    Observation,
    Plan,
    PlayerConfiguration,
    Readiness,
    Revocation,
)
from contracts.time import Clock


class CoordinationLimits(Model):
    horizon_seconds: float = Field(default=300, gt=0, le=3600)
    renewal_seconds: float = Field(default=30, gt=0, le=60)
    prepare_seconds: float = Field(default=5, gt=0, le=30)
    readiness_seconds: float = Field(default=2, gt=0, le=10)
    max_uncertainty: float = Field(default=0.1, gt=0, le=1)
    max_offers: int = Field(default=64, ge=1, le=256)


@dataclass(frozen=True, slots=True)
class DisplayAdmission:
    allowed: bool
    fence: str
    reason: str


class CoordinationError(RegistryError):
    pass


def identity(prefix: str, parts) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return prefix + hashlib.sha256(encoded.encode()).hexdigest()[:40]


def offer_owner(plan: Plan) -> str:
    return f"offer:{plan.player_id}:{plan.authority_epoch}:{plan.revision}"


class Coordinator:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        limits: CoordinationLimits | None = None,
        *,
        media: CoordinationMedia | None = None,
        outcomes: ExecutionOutcomeRouter | None = None,
        installation: InstallationSessions | None = None,
    ):
        self.db, self.clock = db, clock
        self.limits = limits or CoordinationLimits()
        self.runtime = RuntimeStore(db, clock)
        self.media: CoordinationMedia = media or MediaRepository(db, clock, times=DatabaseTransactionClock())
        self.installation = installation or PostgresInstallationRepository(clock)
        self.outcomes = outcomes or ExecutionOutcomeRouter(
            handle_runtime_outcome,
            handle_planning_outcome,
        )

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

    def _authored_scene_profiles(self, conn, scene: Scene):
        frame_ids = {
            contribution.target.removeprefix("frame:")
            for contribution in self._scene_contributions(scene)
            if contribution.asset_refs
        }
        profiles = self.installation.frame_profiles_in(conn, frame_ids)
        if len(profiles) != len(frame_ids):
            raise CoordinationError("unknown_frame", 404)
        return profiles

    def _validate_authored_scene(
        self, conn, scene: Scene, asset_ids: tuple[str, ...], profiles
    ) -> None:
        candidates = self.media.authored_candidates_in(conn, asset_ids)
        for contribution in self._scene_contributions(scene):
            if contribution.asset_refs and not any(
                eligible(candidates[asset_id], profiles[contribution.target.removeprefix("frame:")])
                for asset_id in contribution.asset_refs
            ):
                raise RegistryError("authored_incompatible", 409)

    def configure_authored_scene(
        self, scene: Scene, source_ref: str, asset_ids: tuple[str, ...]
    ) -> dict:
        """Atomically author media refs and adopt their Scene definition."""
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
        with self._transaction() as conn, self.runtime.edit(conn) as runtime:
            # Keep the existing Runtime -> Frame profile -> Media lock order.
            profiles = self._authored_scene_profiles(conn, scene)
            authored = self.media.author_candidates_in(conn, source_ref, requested)
            self._validate_authored_scene(conn, scene, requested, profiles)
            runtime.set_scene(scene)
            return {
                "status": "configured",
                "scene_id": scene.scene_id,
                "revision": scene.revision,
                **authored,
            }

    @contextmanager
    def _transaction(self):
        with self.db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            yield conn

    @contextmanager
    def serialized_runtime_read(self):
        """Hold Coordination and Runtime locks while inspecting current Runtime state."""
        with self._transaction() as conn:
            yield conn, self.runtime.read_locked(conn)

    def _players(self, conn):
        players = self.installation.active_sessions_in(conn)
        if len(players) > 128:
            raise CoordinationError("player_limit")
        return players

    def _configuration(self, conn, player_id: str, epoch: int) -> PlayerConfiguration:
        observed = self.installation.configuration_in(conn, player_id, epoch)
        if observed is None:
            raise CoordinationError("stale_authority", 403)
        content = {
            "bindings": [b.model_dump(mode="json") for b in observed["bindings"]],
            "enabled_outputs": [b.output_id for b in observed["execution_bindings"]],
        }
        old = conn.execute(
            "SELECT * FROM player_configurations WHERE player_id=%s", (player_id,)
        ).fetchone()
        revision = 1
        if old and old["authority_epoch"] == epoch:
            previous = PlayerConfiguration.model_validate(old["configuration"])
            revision = old["revision"] + int(
                content != previous.model_dump(mode="json", include={"bindings", "enabled_outputs"})
            )
        configuration = PlayerConfiguration(
            player_id=player_id, authority_epoch=epoch, configuration_revision=revision, **content
        )
        conn.execute(
            "INSERT INTO player_configurations VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(player_id) DO UPDATE SET authority_epoch=EXCLUDED.authority_epoch,"
            "revision=EXCLUDED.revision,configuration=EXCLUDED.configuration",
            (player_id, epoch, revision, Jsonb(configuration.model_dump(mode="json"))),
        )
        return configuration

    def configuration(self, player_id: str, epoch: int) -> PlayerConfiguration:
        with self._transaction() as conn:
            self._players(conn)
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
            plan = Plan.model_validate(row["manifest"])
            config = configurations.get(plan.player_id)
            # A restarted Player loses execution authority, not the exact
            # content identity of an unexpired, possibly secured assignment.
            if config:
                result[plan.player_id].append(plan)
        return result

    def _locks(self, conn, configs, offers) -> dict[str, Layer]:
        result = {}
        layers = [
            (p, layer) for p, plans in offers.items() for plan in plans for layer in plan.layers
        ]
        for row in conn.execute(
            "SELECT player_id,authority_epoch,layer FROM assignment_locks WHERE valid_until>%s",
            (self.clock.utc(),),
        ).fetchall():
            config = configs.get(row["player_id"])
            if config:
                layers.append((row["player_id"], Layer.model_validate(row["layer"])))
        for player, layer in layers:
            if layer.end <= self.clock.utc() or not self._authorized(layer, configs[player]):
                continue
            old = result.get(layer.assignment_id)
            if old and not same_execution(old, layer):
                raise CoordinationError("inconsistent_content_lock")
            result[layer.assignment_id] = layer
        return result

    def _catalog(self, conn, runtime):
        return self.media.catalog_in(conn, runtime.planning_source_refs())

    def _groups(self, conn, runtime, now, horizon_end, configurations) -> dict[str, str]:
        owners = {
            f"frame:{b.frame_id}": (p, c.authority_epoch, b.output_id, b.generation)
            for p, c in configurations.items()
            for b in c.bindings
            if b.output_id in c.enabled_outputs
        }
        cues: dict[tuple, dict] = {}
        for view in runtime.timeline(now, horizon_end):
            for intent in view.contributions:
                if intent.kind == "actuator" or intent.interval_end <= now:
                    continue
                key = (intent.root_id, intent.interval_start)
                cue = cues.setdefault(key, {"members": {}, "ends": {}, "end": intent.interval_end})
                cue["end"] = max(cue["end"], intent.interval_end)
                cue["members"][assignment_id(intent)] = owners.get(intent.target)
                cue["ends"][assignment_id(intent)] = intent.interval_end
        assignments = {}
        cue_keys = {identity("cue-", list(key)) for key in cues}
        # A future cue is a forecast until it starts; one the timeline no longer holds is
        # withdrawn now rather than surfacing later as a not_ready skip.
        for orphan in conn.execute(
            "SELECT id,cue_key FROM coordination_groups WHERE status IN ('pending','committed') "
            "AND starts_at>%s AND starts_at<%s ORDER BY starts_at,id",
            (now, horizon_end),
        ).fetchall():
            if orphan["cue_key"] not in cue_keys:
                self._skip_group(conn, orphan["id"], "superseded")
        for (root, starts), cue in cues.items():
            cue_key = identity("cue-", [root, starts])
            previous = conn.execute(
                "SELECT * FROM coordination_groups WHERE cue_key=%s "
                "ORDER BY cohort_sequence DESC LIMIT 1",
                (cue_key,),
            ).fetchone()
            superseding = False
            if previous:
                grows = not cue["members"].keys() <= previous["members"].keys()
                moved = not grows and any(
                    (tuple(previous["members"][a]) if previous["members"][a] else None) != owner
                    for a, owner in cue["members"].items()
                )
                if (
                    starts > now
                    and previous["status"] != "skipped"
                    and (moved or cue["members"].keys() != previous["members"].keys())
                ):
                    # An edit rewrote a cue that has not started: a fresh cohort replaces it,
                    # and skipping the old one withdraws any commit it had already granted.
                    self._skip_group(conn, previous["id"], "superseded", root=root)
                    superseding = True
                elif grows:
                    # A started cue is immutable. Growth invalidates this one cue, never
                    # the tick that offers every other Frame its plan.
                    if previous["status"] != "skipped":
                        self._skip_group(conn, previous["id"], "cue_membership_changed", root=root)
                    assignments.update({a: previous["id"] for a in cue["members"]})
                    continue
                elif not moved:
                    # A child completing cannot shrink a skipped cue into a new successful one.
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
                deadline = max(starts, now) + self.limits.prepare_seconds
            else:
                deadline = starts
            conn.execute(
                "INSERT INTO coordination_groups(id,members,starts_at,deadline,valid_until,status,cue_key,member_ends) "
                "VALUES(%s,%s,%s,%s,%s,'pending',%s,%s) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    group_id,
                    Jsonb(cue["members"]),
                    starts,
                    deadline,
                    cue["end"],
                    cue_key,
                    Jsonb(cue["ends"]),
                ),
            )
            assignments.update({a: group_id for a in cue["members"]})
        return assignments

    def _event(self, conn, kind, player=None, assignment=None, detail=None):
        outcome = ExecutionOutcome(
            kind=kind,
            occurred_at=self.clock.utc(),
            player_id=player,
            assignment_id=assignment,
            detail=detail or {},
        )
        handling = self.outcomes.handle(outcome)
        return conn.execute(
            "INSERT INTO execution_events(occurred_at,player_id,assignment_id,kind,detail) "
            "VALUES(%s,%s,%s,%s,%s) RETURNING sequence",
            (
                outcome.occurred_at,
                player,
                assignment,
                kind,
                Jsonb(
                    {
                        **outcome.detail,
                        "handling": handling.model_dump(mode="json"),
                    }
                ),
            ),
        ).fetchone()["sequence"]

    def _skip_group(self, conn, group_id, code, *, cancel=False, root=None):
        cue_key = conn.execute(
            "SELECT cue_key FROM coordination_groups WHERE id=%s", (group_id,)
        ).fetchone()["cue_key"]
        detail = {"group_id": group_id, "code": code, "cue_key": cue_key}
        if root is not None:
            detail["root"] = root
        sequence = self._event(conn, "group_skipped", detail=detail)
        conn.execute(
            "UPDATE coordination_groups SET status='skipped',skip_sequence=%s,drain_cancel=%s "
            "WHERE id=%s",
            (sequence, cancel, group_id),
        )
        conn.execute("UPDATE execution_commits SET valid=FALSE WHERE group_id=%s", (group_id,))

    @staticmethod
    def _plan_groups(conn, plan):
        return conn.execute(
            "SELECT groups FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "AND revision=%s",
            (plan.player_id, plan.authority_epoch, plan.revision),
        ).fetchone()["groups"]

    def _offer(self, conn, plan: Plan, groups: dict[str, str]):
        self.media.pin_variants_in(
            conn,
            (
                MediaPin(
                    owner=offer_owner(plan), variant=layer.variant, expires_at=plan.valid_until
                )
                for layer in plan.layers
                if layer.variant
            ),
            require_ready=True,
        )
        conn.execute(
            "INSERT INTO plan_offers VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                plan.player_id,
                plan.authority_epoch,
                plan.revision,
                plan.plan_id,
                Jsonb(plan.model_dump(mode="json")),
                Jsonb({layer.assignment_id: groups[layer.assignment_id] for layer in plan.layers}),
                plan.issued_at,
                plan.valid_until,
            ),
        )

    def advance(self) -> Projection:
        """One persisted current advance plus pure future planning and atomic offers."""
        with self._transaction() as conn, self.runtime.edit(conn) as runtime:
            # A scheduler waiting on either lock must advance at the committed
            # serialization cut, not at the time it first requested the cut.
            now = self.clock.utc()
            runtime.advance(now)
            fenced = fenced_players_in(conn)
            configurations = {
                p["id"]: self._configuration(conn, p["id"], p["authority_epoch"])
                for p in self._players(conn)
            }
            offers = self._offers(conn, configurations)
            self.media.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            snapshots, authored = self._catalog(conn, runtime)
            locks = self._locks(conn, configurations, offers)
            # Renewable extent changes only at a renewal boundary, avoiding heartbeat churn.
            quantum = self.limits.renewal_seconds
            horizon_end = (math.floor(now / quantum) + 1) * quantum + self.limits.horizon_seconds
            projection = project(
                runtime,
                now,
                bindings_by_player={
                    p: tuple(b for b in config.bindings
                             if p not in fenced and b.output_id in config.enabled_outputs)
                    for p, config in configurations.items()
                },
                catalog_snapshots=snapshots,
                authored_candidates=authored,
                locked_assignments=locks,
                horizon_seconds=horizon_end - now,
                limits=PlannerLimits(max_horizon_seconds=self.limits.horizon_seconds + quantum),
            )
            groups = self._groups(conn, runtime, now, horizon_end, {
                p: config.model_copy(update={"enabled_outputs": ()}) if p in fenced else config
                for p, config in configurations.items()
            })
            for proposal in projection.players:
                config = configurations[proposal.player_id]
                # Historical offers feed content locks only. Reconciliation,
                # revision reuse and backpressure belong to the fresh epoch.
                previous = [
                    plan
                    for plan in offers[proposal.player_id]
                    if plan.authority_epoch == config.authority_epoch
                ]
                latest = previous[-1] if previous else None
                bindings = config.bindings
                membership = {
                    layer.assignment_id: groups[layer.assignment_id] for layer in proposal.layers
                }
                if (
                    latest
                    and (latest.layers, latest.bindings, latest.valid_until)
                    == (proposal.layers, bindings, projection.valid_until)
                    and self._plan_groups(conn, latest) == membership
                ):
                    continue
                if len(previous) >= self.limits.max_offers:
                    self._event(conn, "offer_backpressure", proposal.player_id)
                    continue
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
                )
                try:
                    self._offer(conn, plan, groups)
                except RegistryError as exc:
                    if exc.code != "media_unavailable":
                        raise
                    self._event(
                        conn, "offer_unavailable", proposal.player_id, detail={"code": exc.code}
                    )
            self._commit_due(conn, configurations, now)
            self.media.expire_pins_in(conn, now)
            self._cleanup(
                conn,
                now,
                {
                    (configuration.player_id, configuration.authority_epoch)
                    for configuration in configurations.values()
                },
            )
            return projection

    @staticmethod
    def _cleanup(conn, now, active_sessions: set[tuple[str, int]]):
        """Expiry releases leases, while one latest manifest preserves revision high-water."""
        conn.execute("DELETE FROM assignment_locks WHERE valid_until<=%s", (now,))
        conn.execute(
            "DELETE FROM execution_commits c USING plan_offers o WHERE "
            "(c.player_id,c.authority_epoch,c.revision)=(o.player_id,o.authority_epoch,o.revision) "
            "AND o.valid_until<=%s",
            (now,),
        )
        expired = conn.execute(
            "SELECT player_id,authority_epoch,revision FROM plan_offers WHERE valid_until<=%s",
            (now,),
        ).fetchall()
        for offer in expired:
            session = (offer["player_id"], offer["authority_epoch"])
            newer = conn.execute(
                "SELECT 1 FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                "AND revision>%s",
                (*session, offer["revision"]),
            ).fetchone()
            if session not in active_sessions or newer:
                conn.execute(
                    "DELETE FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                    "AND revision=%s",
                    (*session, offer["revision"]),
                )
        conn.execute(
            "DELETE FROM coordination_groups g WHERE valid_until<=%s "
            "AND NOT EXISTS(SELECT 1 FROM execution_commits c WHERE c.group_id=g.id)",
            (now,),
        )
        for feedback in conn.execute(
            "SELECT player_id,authority_epoch FROM player_feedback"
        ).fetchall():
            session = (feedback["player_id"], feedback["authority_epoch"])
            if session not in active_sessions:
                conn.execute(
                    "DELETE FROM player_feedback WHERE player_id=%s AND authority_epoch=%s",
                    session,
                )
        conn.execute(
            "DELETE FROM execution_events WHERE sequence<(SELECT COALESCE(max(sequence),0)-10000 "
            "FROM execution_events)"
        )

    def _current_plan(self, conn, player_id, epoch) -> Plan | None:
        row = conn.execute(
            "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "ORDER BY revision DESC LIMIT 1",
            (player_id, epoch),
        ).fetchone()
        return Plan.model_validate(row["manifest"]) if row else None

    def delivery(self, player_id: str, epoch: int, *, finalize=None) -> dict:
        """Read delivery and optionally finalize its wire challenge under one lock cut."""
        with self._transaction() as conn:
            self._players(conn)
            config = self._configuration(conn, player_id, epoch)
            now = self.clock.utc()
            identify = conn.execute(
                "SELECT i.request_id,i.output_id,i.authority_epoch,i.expires_at,p.authority_epoch "
                "AS current_epoch,p.retired_at,o.observation,"
                "EXISTS(SELECT 1 FROM bindings b WHERE b.player_id=i.player_id "
                "AND b.output_id=i.output_id) AS is_bound "
                "FROM player_output_identification i JOIN players p ON p.id=i.player_id "
                "LEFT JOIN outputs o ON o.player_id=i.player_id AND o.output_id=i.output_id "
                "WHERE i.player_id=%s",
                (player_id,),
            ).fetchone()
            identify_output = None
            if identify:
                remaining = identify["expires_at"] - now
                connected = bool(identify["observation"] and
                                 identify["observation"].get("connected", False))
                if (identify["retired_at"] is None and identify["current_epoch"] == epoch
                        and identify["authority_epoch"] == epoch and remaining > 0
                        and connected and not identify["is_bound"]):
                    identify_output = IdentifyOutput(
                        request_id=str(identify["request_id"]),
                        output_id=identify["output_id"],
                        authority_epoch=identify["authority_epoch"],
                        remaining_seconds=min(15.0, remaining),
                    )
                else:
                    conn.execute(
                        "DELETE FROM player_output_identification WHERE player_id=%s "
                        "AND request_id=%s",
                        (player_id, identify["request_id"]),
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
                    "SELECT id,skip_sequence,drain_cancel FROM coordination_groups "
                    "WHERE status='skipped' AND skip_sequence IS NOT NULL"
                ).fetchall()
                revocations = tuple(
                    Revocation(
                        plan_id=plan.plan_id,
                        revision=plan.revision,
                        authority_epoch=epoch,
                        sequence=g["skip_sequence"],
                        mode="cancel" if g["drain_cancel"] else "invalidate",
                        assignment_ids=tuple(
                            sorted(a for a, group in membership.items() if group == g["id"])
                        ),
                    )
                    for g in sorted(skipped, key=lambda g: g["skip_sequence"])
                    if g["id"] in membership.values()
                )
                rows = conn.execute(
                    "SELECT assignment_id,committed_at,readiness_sequence FROM execution_commits "
                    "WHERE player_id=%s AND authority_epoch=%s AND revision=%s AND valid",
                    (player_id, epoch, plan.revision),
                ).fetchall()
                if rows:
                    # Groups can have been granted against different Player snapshots.
                    commits = tuple(
                        Commit(
                            plan_id=plan.plan_id,
                            revision=plan.revision,
                            authority_epoch=epoch,
                            readiness_sequence=sequence,
                            assignment_ids=tuple(
                                sorted(
                                    r["assignment_id"]
                                    for r in rows
                                    if r["readiness_sequence"] == sequence
                                )
                            ),
                            committed_at=max(
                                r["committed_at"]
                                for r in rows
                                if r["readiness_sequence"] == sequence
                            ),
                        )
                        for sequence in sorted({r["readiness_sequence"] for r in rows})
                    )
            delivery = {
                "configuration": config,
                "plan": plan,
                "commits": commits,
                "revocations": revocations,
                "identify_output": identify_output,
            }
            return finalize(conn, delivery) if finalize is not None else delivery

    def player_reports_lock_free(self) -> PlayerReports:
        """The Players' last accepted reports, read in one statement of a plain transaction.

        This never takes COORDINATION_LOCK, so it answers while advance() or readiness() holds
        it. A report from an earlier authority epoch, or from a retired Player, is not counted:
        a Player is only heard once it reports on its current session."""
        with self.db.transaction() as conn:
            return self.player_reports_in(conn, self.clock.utc())

    @staticmethod
    def player_reports_in(conn, read_at: float) -> PlayerReports:
        """Read accepted reports from a caller-owned transaction and label their read time."""
        rows = conn.execute(
            "SELECT f.player_id, f.received_at FROM players p JOIN player_feedback f "
            "ON f.player_id=p.id AND f.authority_epoch=p.authority_epoch "
            "WHERE p.retired_at IS NULL"
        ).fetchall()
        reports = {row["player_id"]: row["received_at"] for row in rows}
        return PlayerReports(read_at=max((read_at, *reports.values())), reports=reports)

    @staticmethod
    def output_interruptions_in(conn) -> tuple[OutputInterruption, ...]:
        """Unresolved Output losses that fence a current Binding, from a caller-owned transaction.

        A loss is keyed by (player, authority epoch, Output, Frame, binding generation) and fences
        only on that exact key (`_commit_due`). A row from an earlier epoch, an earlier binding
        generation or another Frame on the same Output is therefore not served: a reader can
        never attach it to whatever Frame is bound there now. Read-only; takes no lock."""
        rows = conn.execute(
            "SELECT l.frame_id,l.player_id,l.output_id,l.binding_generation,"
            "pr.owner AS cause_layer,l.interrupted_at FROM node_output_losses l "
            "JOIN players p ON p.id=l.player_id AND p.authority_epoch=l.authority_epoch "
            "JOIN bindings b ON b.frame_id=l.frame_id AND b.player_id=l.player_id "
            "AND b.output_id=l.output_id "
            "JOIN frames f ON f.id=l.frame_id AND f.generation=l.binding_generation "
            "JOIN node_producers pr ON pr.producer_id=l.cause_producer_id "
            "WHERE l.resolved_at IS NULL ORDER BY l.frame_id"
        ).fetchall()
        return tuple(OutputInterruption.model_validate(dict(row)) for row in rows)

    def readiness(self, player_id: str, report: Readiness) -> bool:
        now = self.clock.utc()
        with self._transaction() as conn:
            if player_id in fenced_players_in(conn):
                raise CoordinationError("equipment_draining")
            configurations = {
                p["id"]: self._configuration(conn, p["id"], p["authority_epoch"])
                for p in self._players(conn)
            }
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
            old = conn.execute(
                "SELECT sequence FROM player_feedback WHERE player_id=%s AND authority_epoch=%s",
                (player_id, report.authority_epoch),
            ).fetchone()
            if old and report.sequence <= old["sequence"]:
                return False
            if report.observed_at > now + self.limits.max_uncertainty:
                raise CoordinationError("future_readiness")
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
            pins = []
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
                    "INSERT INTO assignment_locks VALUES(%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(player_id,authority_epoch,assignment_id) DO NOTHING",
                    (
                        player_id,
                        report.authority_epoch,
                        assignment,
                        Jsonb(layer.model_dump(mode="json")),
                        now,
                        layer.end,
                    ),
                )
                if layer.variant:
                    pins.append(
                        MediaPin(
                            owner=f"secured:{player_id}:{report.authority_epoch}:{assignment}",
                            variant=layer.variant,
                            expires_at=layer.end,
                        )
                    )
            self.media.pin_variants_in(conn, pins, require_ready=True)
            conn.execute(
                "INSERT INTO player_feedback VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT(player_id,authority_epoch) DO UPDATE SET sequence=EXCLUDED.sequence,"
                "received_at=EXCLUDED.received_at,readiness=EXCLUDED.readiness",
                (
                    player_id,
                    report.authority_epoch,
                    report.sequence,
                    now,
                    Jsonb(report.model_dump(mode="json")),
                ),
            )
            failed = {f.assignment_id: f.code for f in report.failures}
            # Loss is local first on the Player; this persists central invalidation.
            current = self._current_plan(conn, player_id, report.authority_epoch)
            if (
                current
                and current.revision == report.revision
                and current.bindings == config.bindings
            ):
                for layer in current.layers:
                    if (
                        layer.assignment_id not in report.prepared
                        or not report.capacity_ok
                        or report.clock_uncertainty > self.limits.max_uncertainty
                    ):
                        groups = conn.execute(
                            "SELECT DISTINCT g.id FROM execution_commits c "
                            "JOIN coordination_groups g ON g.id=c.group_id "
                            "WHERE c.player_id=%s AND c.authority_epoch=%s "
                            "AND c.assignment_id=%s AND c.valid AND g.starts_at>%s",
                            (player_id, report.authority_epoch, layer.assignment_id, now),
                        ).fetchall()
                        for group in groups:
                            self._skip_group(conn, group["id"], "readiness_lost")
                        conn.execute(
                            "UPDATE execution_commits SET valid=FALSE WHERE player_id=%s "
                            "AND authority_epoch=%s AND assignment_id=%s",
                            (player_id, report.authority_epoch, layer.assignment_id),
                        )
            for assignment, code in failed.items():
                self._event(conn, "readiness_lost", player_id, assignment, {"code": code})
            self._commit_due(conn, configurations, now)
            return True

    def display_admission_in(self, conn, *, player_id: str, authority_epoch: int,
                             output_id: str, frame_id: str, binding_generation: int, config_revision: int,
                             process, app_epoch: int, environment_sha256: str,
                             phase: str) -> DisplayAdmission:
        """Authorize an exact operational surface, never an execution Commit.

        Caller holds Coordination→Runtime before authenticating the node session.
        A live app linkage and Registry tuple are necessary. V1 drain rows cannot
        authorize V2 starting-new candidates; that requires the V2 lifecycle port.
        """
        from central.fleet.node_app_links import load_current_node_app_link_for_player_in

        if not holds_runtime_locks_in(conn):
            raise ValueError("node_display_runtime_locks_required")
        if phase not in ("candidate", "handoff", "revision"):
            raise ValueError("node_display_phase_invalid")
        observed = self.installation.configuration_in(conn, player_id, authority_epoch)
        if observed is None or not any(b.output_id == output_id and b.frame_id == frame_id
                and b.generation == binding_generation
                and b.configuration_revision == config_revision for b in observed["bindings"]):
            return DisplayAdmission(False, "", "registry_output_changed")
        link = load_current_node_app_link_for_player_in(conn, player_id, authority_epoch, self.clock.utc())
        if link is None or (link.process, link.app_epoch, link.environment_sha256) != (
                process, app_epoch, environment_sha256):
            return DisplayAdmission(False, "", "app_process_link_changed")
        if player_id in fenced_players_in(conn):
            return DisplayAdmission(False, "", "equipment_drain_requires_v2_target_reservation")
        fence = identity("display-", [player_id, authority_epoch, output_id, frame_id, binding_generation,
            config_revision, link.producer.device_generation, str(link.producer.kernel_boot_id),
            link.process.pid, link.process.start_ticks, str(link.process.invocation_id),
            link.app_epoch, link.environment_sha256, phase])
        return DisplayAdmission(True, fence, "current_linked_app")

    def display_withdrawal_in(self, conn, *, player_id: str, authority_epoch: int,
                              previous_surface, expected_surface) -> DisplayAdmission:
        """Revoke exactly an old operational role after Registry authority changes.

        Caller proves this DisplayHost previously admitted the old surface. This
        decision neither changes Run state nor grants a replacement surface.
        """
        from central.fleet.node_app_links import load_current_node_app_link_for_player_in

        if not holds_runtime_locks_in(conn):
            raise ValueError("node_display_runtime_locks_required")
        if previous_surface.frame_id is None:
            return DisplayAdmission(False, "", "historical_surface_identity_incomplete")
        observed = self.installation.configuration_in(conn, player_id, authority_epoch)
        if observed is None:
            return DisplayAdmission(False, "", "registry_player_changed")
        output_id = previous_surface.output.output_id
        binding = next((b for b in observed["bindings"] if b.output_id == output_id), None)
        if expected_surface is None:
            if binding is not None:
                return DisplayAdmission(False, "", "registry_output_still_bound")
        else:
            if (binding is None or expected_surface.output.output_id != output_id
                    or (binding.frame_id, binding.generation, binding.configuration_revision) != (
                        expected_surface.frame_id, expected_surface.binding_generation, expected_surface.config_revision)):
                return DisplayAdmission(False, "", "registry_output_changed")
            link = load_current_node_app_link_for_player_in(conn, player_id, authority_epoch, self.clock.utc())
            if link is None or (link.process, link.app_epoch) != (expected_surface.process, expected_surface.app_epoch):
                return DisplayAdmission(False, "", "app_process_link_changed")
            if previous_surface == expected_surface:
                return DisplayAdmission(False, "", "surface_still_current")
        def parts(surface):
            if surface is None:
                return None
            return [surface.frame_id, surface.output.output_id, str(surface.output.kernel_boot_id),
                    str(surface.output.display_host_incarnation), surface.output.connection_generation,
                    surface.output.mode_generation, surface.process.pid, surface.process.start_ticks,
                    str(surface.process.invocation_id), surface.app_epoch, surface.binding_generation,
                    surface.config_revision]
        fence = identity("withdraw-", [player_id, authority_epoch, parts(previous_surface), parts(expected_surface)])
        return DisplayAdmission(True, fence, "old_registry_surface_revoked")

    def reconcile_node_output_in(self, conn, *, player_id: str, authority_epoch: int,
                                 output_id: str, binding_generation: int,
                                 configuration_revision: int, recovering: bool, frame_id: str | None = None,
                                 producer_id, evidence_id, evidence_kind: str, detail: dict) -> bool:
        """Apply one affirmatively linked Output fact without changing its Run.

        Caller holds Coordination→Runtime→Fleet locks and owns the inbox transaction.
        This port rechecks Registry epoch/binding; no other Frame or Actuator is commanded.
        """
        if not holds_runtime_locks_in(conn):
            raise ValueError("node_reconciliation_runtime_locks_required")
        observed = self.installation.configuration_in(conn, player_id, authority_epoch)
        if observed is None:
            return False
        binding = next((b for b in observed["bindings"] if b.output_id == output_id
                        and b.frame_id == frame_id and b.generation == binding_generation
                        and b.configuration_revision == configuration_revision), None)
        if binding is None:
            return False
        now = self.clock.utc()
        existing = conn.execute("SELECT resolved_at FROM node_output_losses WHERE player_id=%s "
                                "AND authority_epoch=%s AND output_id=%s AND frame_id=%s AND binding_generation=%s",
                                (player_id, authority_epoch, output_id, frame_id, binding_generation)).fetchone()
        if recovering:
            if existing is None or existing["resolved_at"] is not None:
                return False
            conn.execute("UPDATE node_output_losses SET resolved_at=%s WHERE player_id=%s "
                         "AND authority_epoch=%s AND output_id=%s AND frame_id=%s AND binding_generation=%s",
                         (now, player_id, authority_epoch, output_id, frame_id, binding_generation))
        else:
            if existing is not None and existing["resolved_at"] is None:
                return False
            conn.execute("INSERT INTO node_output_losses(player_id,authority_epoch,output_id,frame_id,"
                         "binding_generation,configuration_revision,cause_producer_id,cause_evidence_id,"
                         "cause_kind,detail,interrupted_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                         "ON CONFLICT(player_id,authority_epoch,output_id,frame_id,binding_generation) DO UPDATE "
                         "SET configuration_revision=EXCLUDED.configuration_revision,"
                         "cause_producer_id=EXCLUDED.cause_producer_id,cause_evidence_id=EXCLUDED.cause_evidence_id,"
                         "cause_kind=EXCLUDED.cause_kind,detail=EXCLUDED.detail,"
                         "interrupted_at=EXCLUDED.interrupted_at,resolved_at=NULL",
                         (player_id, authority_epoch, output_id, frame_id, binding_generation,
                          configuration_revision, producer_id, evidence_id, evidence_kind, Jsonb(detail), now))
            # Invalidate only layers on this exact Output binding, across retained revisions.
            offers = conn.execute("SELECT revision,manifest FROM plan_offers WHERE player_id=%s "
                                  "AND authority_epoch=%s", (player_id, authority_epoch)).fetchall()
            for row in offers:
                plan = Plan.model_validate(row["manifest"])
                assignments = [layer.assignment_id for layer in plan.layers if layer.output_id == output_id
                               and layer.frame_id == frame_id and layer.binding_generation == binding_generation]
                if assignments:
                    conn.execute("UPDATE execution_commits SET valid=FALSE WHERE player_id=%s "
                                 "AND authority_epoch=%s AND revision=%s AND assignment_id=ANY(%s)",
                                 (player_id, authority_epoch, row["revision"], assignments))
        self._event(conn, "observation", player_id, detail={**detail, "output_id": output_id,
                    "frame_id": binding.frame_id, "binding_generation": binding_generation,
                    "authority_epoch": authority_epoch, "run_lifecycle": "continues"})
        return True

    def _commit_due(self, conn, configurations, now):
        interrupted = {(row["player_id"], row["authority_epoch"], row["output_id"], row["frame_id"], row["binding_generation"])
                       for row in conn.execute("SELECT player_id,authority_epoch,output_id,frame_id,binding_generation "
                                               "FROM node_output_losses WHERE resolved_at IS NULL").fetchall()}
        plans = {
            p: self._current_plan(conn, p, c.authority_epoch) for p, c in configurations.items()
        }
        plan_groups = {p: self._plan_groups(conn, plan) for p, plan in plans.items() if plan}
        feedback = {}
        for row in conn.execute("SELECT * FROM player_feedback").fetchall():
            config = configurations.get(row["player_id"])
            if config and config.authority_epoch == row["authority_epoch"]:
                feedback[row["player_id"]] = (row, Readiness.model_validate(row["readiness"]))
        for group in conn.execute(
            "SELECT * FROM coordination_groups WHERE status!='skipped' "
            "AND valid_until>%s AND starts_at<=%s ORDER BY starts_at,id",
            (now, now + self.limits.prepare_seconds),
        ).fetchall():
            if group["status"] == "pending" and now > group["deadline"]:
                self._skip_group(conn, group["id"], "not_ready")
                continue
            ready, participants = True, []
            for assignment, owner in group["members"].items():
                if (
                    group["status"] == "committed"
                    and group["member_ends"].get(assignment, group["valid_until"]) <= now
                ):
                    continue
                if owner is None:
                    ready = False
                    break
                player, epoch, output, generation = owner
                plan, state = plans.get(player), feedback.get(player)
                layer = (
                    next((a for a in plan.layers if a.assignment_id == assignment), None)
                    if plan
                    else None
                )
                if (
                    not plan
                    or not state
                    or not layer
                    or (player, epoch, output, layer.frame_id, generation) in interrupted
                    or not self._authorized(layer, configurations[player])
                    or plan.bindings != configurations[player].bindings
                    or plan.valid_until <= now
                    or plan_groups[player].get(assignment) != group["id"]
                ):
                    ready = False
                    break
                row, report = state
                if (
                    plan.authority_epoch != epoch
                    or plan.revision != report.revision
                    or plan.plan_id != report.plan_id
                    or assignment not in report.prepared
                    or not report.capacity_ok
                    or report.clock_uncertainty > self.limits.max_uncertainty
                    or now - report.observed_at > self.limits.readiness_seconds
                    or now - row["received_at"] > self.limits.readiness_seconds
                    or layer.output_id != output
                    or layer.binding_generation != generation
                ):
                    ready = False
                    break
                participants.append((plan, layer, report.sequence))
            if ready and participants:
                for plan, layer, sequence in participants:
                    conn.execute(
                        "INSERT INTO execution_commits VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s) "
                        "ON CONFLICT(player_id,authority_epoch,revision,assignment_id) "
                        "DO UPDATE SET valid=TRUE,committed_at=EXCLUDED.committed_at,"
                        "readiness_sequence=EXCLUDED.readiness_sequence",
                        (
                            plan.player_id,
                            plan.authority_epoch,
                            plan.revision,
                            layer.assignment_id,
                            group["id"],
                            now,
                            sequence,
                        ),
                    )
                conn.execute(
                    "UPDATE coordination_groups SET status='committed' WHERE id=%s", (group["id"],)
                )
            elif group["status"] == "pending" and now >= group["deadline"]:
                self._skip_group(conn, group["id"], "not_ready")

    def observe(self, player_id: str, observation: Observation):
        with self._transaction() as conn:
            self._players(conn)
            config = self._configuration(conn, player_id, observation.authority_epoch)
            plan = self._current_plan(conn, player_id, observation.authority_epoch)
            if not plan or (plan.plan_id, plan.revision) != (
                observation.plan_id,
                observation.revision,
            ):
                raise CoordinationError("stale_observation")
            layer = next(
                (a for a in plan.layers if a.assignment_id == observation.assignment_id), None
            )
            if not layer or not self._authorized(layer, config):
                raise CoordinationError("stale_binding", 403)
            self._event(
                conn,
                "observation",
                player_id,
                layer.assignment_id,
                observation.model_dump(mode="json"),
            )
