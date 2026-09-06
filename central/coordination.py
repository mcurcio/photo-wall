"""Persisted offers, byte locks and revocable group execution authority.

No upstream transport, conversion or renderer work occurs in these transactions.
See docs/module-coordination.md for lock order and conservative offer leases.
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager

from psycopg.types.json import Jsonb
from pydantic import Field

from central.db import MEDIA_LOCK, Database
from central.media_repository import MediaRepository
from central.planner import PlannerLimits, Projection, assignment_id, eligible, project
from central.registry import Registry, RegistryError
from central.runtime import Scene
from central.runtime_store import RuntimeStore
from contracts.models import (
    Commit,
    Layer,
    Model,
    Observation,
    Plan,
    PlayerConfiguration,
    Readiness,
    Revocation,
)
from contracts.time import Clock

COORDINATION_LOCK = 734118324


class CoordinationLimits(Model):
    horizon_seconds: float = Field(default=300, gt=0, le=3600)
    renewal_seconds: float = Field(default=30, gt=0, le=60)
    prepare_seconds: float = Field(default=5, gt=0, le=30)
    readiness_seconds: float = Field(default=2, gt=0, le=10)
    max_uncertainty: float = Field(default=.1, gt=0, le=1)
    max_offers: int = Field(default=64, ge=1, le=256)


class CoordinationError(RegistryError):
    pass


def identity(prefix: str, parts) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return prefix + hashlib.sha256(encoded.encode()).hexdigest()[:40]


def same_execution(first: Layer, second: Layer) -> bool:
    # Arbitration can change while the adopted media/interval remains immutable.
    exclude = {"priority", "root_order", "admission_order"}
    return first.model_dump(exclude=exclude) == second.model_dump(exclude=exclude)


def offer_owner(plan: Plan) -> str:
    return f"offer:{plan.player_id}:{plan.authority_epoch}:{plan.revision}"


class Coordinator:
    def __init__(self, db: Database, clock: Clock, limits: CoordinationLimits | None = None):
        self.db, self.clock = db, clock
        self.limits = limits or CoordinationLimits()
        self.registry, self.runtime = Registry(db, clock), RuntimeStore(db, clock)
        self.media = MediaRepository(db, clock)

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
        return self.registry.frame_profiles_in(conn, frame_ids)

    def _validate_authored_scene(self, conn, scene: Scene, asset_ids: tuple[str, ...], profiles) -> None:
        candidates = self.media._authored_candidates_in(conn, asset_ids)
        for contribution in self._scene_contributions(scene):
            if contribution.asset_refs and not any(
                eligible(candidates[asset_id], profiles[contribution.target.removeprefix("frame:")])
                for asset_id in contribution.asset_refs
            ):
                raise RegistryError("authored_incompatible", 409)

    def configure_authored_scene(self, scene: Scene, source_ref: str,
                                 asset_ids: tuple[str, ...]) -> dict:
        """Atomically author media refs and adopt their Scene definition."""
        try:
            requested = tuple(asset_ids)
            requested_set = set(requested)
        except (TypeError, ValueError) as exc:
            raise RegistryError("invalid_authored_scene", 422) from exc
        scene_refs, has_source_refs = self._authored_scene_refs(scene)
        if (not requested or len(requested) != len(requested_set) or
                requested_set != scene_refs or has_source_refs):
            raise RegistryError("invalid_authored_scene", 422)
        with self._transaction() as conn, self.runtime.edit(conn) as runtime:
            # Keep the existing Runtime -> Frame profile -> MEDIA lock order.
            profiles = self._authored_scene_profiles(conn, scene)
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
            authored = self.media._author_authored_candidates_in(conn, source_ref, requested)
            self._validate_authored_scene(conn, scene, requested, profiles)
            runtime.set_scene(scene)
            return {"status": "configured", "scene_id": scene.scene_id,
                    "revision": scene.revision, **authored}

    @contextmanager
    def _transaction(self):
        with self.db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            yield conn

    @staticmethod
    def _players(conn):
        players = conn.execute("SELECT id,authority_epoch FROM players WHERE retired_at IS NULL "
                               "ORDER BY id FOR SHARE").fetchall()
        if len(players) > 128:
            raise CoordinationError("player_limit")
        return players

    def _configuration(self, conn, player_id: str, epoch: int) -> PlayerConfiguration:
        observed = self.registry.configuration_in(conn, player_id, epoch)
        content = {"bindings": [b.model_dump(mode="json") for b in observed["bindings"]],
                   "enabled_outputs": [b.output_id for b in observed["execution_bindings"]]}
        old = conn.execute("SELECT * FROM player_configurations WHERE player_id=%s",
                           (player_id,)).fetchone()
        revision = 1
        if old and old["authority_epoch"] == epoch:
            previous = PlayerConfiguration.model_validate(old["configuration"])
            revision = old["revision"] + int(content != previous.model_dump(
                mode="json", include={"bindings", "enabled_outputs"}))
        configuration = PlayerConfiguration(player_id=player_id, authority_epoch=epoch,
                                              configuration_revision=revision, **content)
        conn.execute("INSERT INTO player_configurations VALUES(%s,%s,%s,%s) "
                     "ON CONFLICT(player_id) DO UPDATE SET authority_epoch=EXCLUDED.authority_epoch,"
                     "revision=EXCLUDED.revision,configuration=EXCLUDED.configuration",
                     (player_id, epoch, revision, Jsonb(configuration.model_dump(mode="json"))))
        return configuration

    def configuration(self, player_id: str, epoch: int) -> PlayerConfiguration:
        with self._transaction() as conn:
            self._players(conn)
            return self._configuration(conn, player_id, epoch)

    @staticmethod
    def _authorized(layer: Layer, configuration: PlayerConfiguration) -> bool:
        return configuration.authorizes(layer)

    def _offers(self, conn, configurations: dict[str, PlayerConfiguration]) -> dict[str, list[Plan]]:
        result = {p: [] for p in configurations}
        for row in conn.execute("SELECT manifest FROM plan_offers WHERE valid_until>%s "
                                "ORDER BY revision", (self.clock.utc(),)).fetchall():
            plan = Plan.model_validate(row["manifest"])
            config = configurations.get(plan.player_id)
            # A restarted Player loses execution authority, not the exact
            # content identity of an unexpired, possibly secured assignment.
            if config:
                result[plan.player_id].append(plan)
        return result

    def _locks(self, conn, configs, offers) -> dict[str, Layer]:
        result = {}
        layers = [(p, layer) for p, plans in offers.items() for plan in plans for layer in plan.layers]
        for row in conn.execute("SELECT player_id,authority_epoch,layer FROM assignment_locks "
                                "WHERE valid_until>%s", (self.clock.utc(),)).fetchall():
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

    def _catalog(self, conn):
        return self.media.catalog_in(conn, self.clock.utc())

    def _groups(self, conn, runtime, now, horizon_end, configurations) -> dict[str, str]:
        owners = {f"frame:{b.frame_id}": (p, c.authority_epoch, b.output_id, b.generation)
                  for p, c in configurations.items() for b in c.bindings
                  if b.output_id in c.enabled_outputs}
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
        for (root, starts), cue in cues.items():
            cue_key = identity("cue-", [root, starts])
            previous = conn.execute("SELECT * FROM coordination_groups WHERE cue_key=%s "
                                    "ORDER BY cohort_sequence DESC LIMIT 1", (cue_key,)).fetchone()
            if previous:
                if not cue["members"].keys() <= previous["members"].keys():
                    raise CoordinationError("cue_membership_changed")
                # A child completing cannot shrink a skipped cue into a new successful one.
                changed_authority = any(
                    (tuple(previous["members"][a]) if previous["members"][a] else None) != owner
                    for a, owner in cue["members"].items())
                if not changed_authority:
                    assignments.update({a: previous["id"] for a in cue["members"]})
                    continue
            group_id = identity("group-", [cue_key, cue["members"]])
            deadline = now + self.limits.prepare_seconds if starts <= now else starts
            conn.execute("INSERT INTO coordination_groups(id,members,starts_at,deadline,valid_until,status,cue_key,member_ends) "
                         "VALUES(%s,%s,%s,%s,%s,'pending',%s,%s) "
                         "ON CONFLICT(id) DO NOTHING",
                         (group_id, Jsonb(cue["members"]), starts, deadline, cue["end"], cue_key, Jsonb(cue["ends"])))
            assignments.update({a: group_id for a in cue["members"]})
        return assignments

    def _event(self, conn, kind, player=None, assignment=None, detail=None):
        return conn.execute("INSERT INTO execution_events(occurred_at,player_id,assignment_id,kind,detail) "
                            "VALUES(%s,%s,%s,%s,%s) RETURNING sequence",
                            (self.clock.utc(), player, assignment, kind, Jsonb(detail or {}))).fetchone()["sequence"]

    def _skip_group(self, conn, group_id, code):
        sequence = self._event(conn, "group_skipped", detail={"group_id": group_id, "code": code})
        conn.execute("UPDATE coordination_groups SET status='skipped',skip_sequence=%s WHERE id=%s", (sequence, group_id))
        conn.execute("UPDATE execution_commits SET valid=FALSE WHERE group_id=%s", (group_id,))

    @staticmethod
    def _plan_groups(conn, plan):
        return conn.execute("SELECT groups FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                            "AND revision=%s", (plan.player_id, plan.authority_epoch, plan.revision)).fetchone()["groups"]

    def _offer(self, conn, plan: Plan, groups: dict[str, str]):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        variants = {layer.variant.sha256: layer.variant for layer in plan.layers if layer.variant}
        # Complete validation precedes reference writes, so a refused offer is atomic.
        for digest, variant in variants.items():
            blob = conn.execute("SELECT variant FROM media_blobs WHERE digest=%s AND state='ready'",
                                (digest,)).fetchone()
            if not blob or blob["variant"] != variant.model_dump(mode="json"):
                raise CoordinationError("media_unavailable")
        for digest in variants:
            conn.execute("INSERT INTO media_references VALUES(%s,%s,%s) "
                         "ON CONFLICT(owner,digest) DO UPDATE SET expires_at=EXCLUDED.expires_at",
                         (offer_owner(plan), digest, plan.valid_until))
        conn.execute("INSERT INTO plan_offers VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                     (plan.player_id, plan.authority_epoch, plan.revision, plan.plan_id,
                      Jsonb(plan.model_dump(mode="json")),
                      Jsonb({layer.assignment_id: groups[layer.assignment_id] for layer in plan.layers}),
                      plan.issued_at, plan.valid_until))

    def advance(self) -> Projection:
        """One persisted current advance plus pure future planning and atomic offers."""
        now = self.clock.utc()
        with self._transaction() as conn, self.runtime.edit(conn) as runtime:
            runtime.advance(now)
            configurations = {p["id"]: self._configuration(conn, p["id"], p["authority_epoch"])
                              for p in self._players(conn)}
            offers = self._offers(conn, configurations)
            snapshots, authored = self._catalog(conn)
            locks = self._locks(conn, configurations, offers)
            # Renewable extent changes only at a renewal boundary, avoiding heartbeat churn.
            quantum = self.limits.renewal_seconds
            horizon_end = (math.floor(now / quantum) + 1) * quantum + self.limits.horizon_seconds
            projection = project(runtime, now, bindings_by_player={
                p: tuple(b for b in config.bindings if b.output_id in config.enabled_outputs)
                for p, config in configurations.items()}, catalog_snapshots=snapshots,
                authored_candidates=authored, locked_assignments=locks,
                horizon_seconds=horizon_end - now, limits=PlannerLimits(
                    max_horizon_seconds=self.limits.horizon_seconds + quantum))
            groups = self._groups(conn, runtime, now, horizon_end, configurations)
            for proposal in projection.players:
                config = configurations[proposal.player_id]
                # Historical offers feed content locks only. Reconciliation,
                # revision reuse and backpressure belong to the fresh epoch.
                previous = [plan for plan in offers[proposal.player_id]
                            if plan.authority_epoch == config.authority_epoch]
                latest = previous[-1] if previous else None
                bindings = config.bindings
                membership = {layer.assignment_id: groups[layer.assignment_id] for layer in proposal.layers}
                if latest and (latest.layers, latest.bindings, latest.valid_until) == (
                    proposal.layers, bindings, projection.valid_until
                ) and self._plan_groups(conn, latest) == membership:
                    continue
                if len(previous) >= self.limits.max_offers:
                    self._event(conn, "offer_backpressure", proposal.player_id)
                    continue
                revision = conn.execute("SELECT COALESCE(max(revision),0)+1 AS revision FROM plan_offers "
                                        "WHERE player_id=%s AND authority_epoch=%s",
                                        (proposal.player_id, config.authority_epoch)).fetchone()["revision"]
                plan = Plan(plan_id=identity("plan-", [proposal.player_id, config.authority_epoch]),
                            revision=revision, player_id=proposal.player_id,
                            authority_epoch=config.authority_epoch, issued_at=now, valid_from=now,
                            valid_until=projection.valid_until, bindings=bindings, layers=proposal.layers)
                try:
                    self._offer(conn, plan, groups)
                except CoordinationError as exc:
                    if exc.code != "media_unavailable":
                        raise
                    self._event(conn, "offer_unavailable", proposal.player_id, detail={"code": exc.code})
            self._commit_due(conn, configurations, now)
            self._cleanup(conn, now)
            return projection

    @staticmethod
    def _cleanup(conn, now):
        """Expiry releases leases, while one latest manifest preserves revision high-water."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        conn.execute("DELETE FROM media_references WHERE expires_at<=%s", (now,))
        conn.execute("DELETE FROM assignment_locks WHERE valid_until<=%s", (now,))
        conn.execute("DELETE FROM execution_commits c USING plan_offers o WHERE "
                     "(c.player_id,c.authority_epoch,c.revision)=(o.player_id,o.authority_epoch,o.revision) "
                     "AND o.valid_until<=%s", (now,))
        conn.execute("DELETE FROM plan_offers o WHERE o.valid_until<=%s AND (EXISTS "
                     "(SELECT 1 FROM plan_offers newer WHERE newer.player_id=o.player_id "
                     "AND newer.authority_epoch=o.authority_epoch AND newer.revision>o.revision) "
                     "OR NOT EXISTS(SELECT 1 FROM players p WHERE p.id=o.player_id "
                     "AND p.authority_epoch=o.authority_epoch AND p.retired_at IS NULL))", (now,))
        conn.execute("DELETE FROM coordination_groups g WHERE valid_until<=%s "
                     "AND NOT EXISTS(SELECT 1 FROM execution_commits c WHERE c.group_id=g.id)", (now,))
        conn.execute("DELETE FROM player_feedback f WHERE NOT EXISTS(SELECT 1 FROM players p "
                     "WHERE p.id=f.player_id AND p.authority_epoch=f.authority_epoch AND p.retired_at IS NULL)")
        conn.execute("DELETE FROM execution_events WHERE sequence<(SELECT COALESCE(max(sequence),0)-10000 "
                     "FROM execution_events)")

    def _current_plan(self, conn, player_id, epoch) -> Plan | None:
        row = conn.execute("SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                           "ORDER BY revision DESC LIMIT 1", (player_id, epoch)).fetchone()
        return Plan.model_validate(row["manifest"]) if row else None

    def delivery(self, player_id: str, epoch: int) -> dict:
        with self._transaction() as conn:
            self._players(conn)
            config = self._configuration(conn, player_id, epoch)
            plan = self._current_plan(conn, player_id, epoch)
            if plan and (plan.valid_until <= self.clock.utc() or
                         plan.bindings != config.bindings or
                         any(not self._authorized(layer, config) for layer in plan.layers)):
                plan = None
            commits, revocations = (), ()
            if plan:
                membership = self._plan_groups(conn, plan)
                skipped = conn.execute("SELECT id,skip_sequence FROM coordination_groups "
                                       "WHERE status='skipped' AND skip_sequence IS NOT NULL").fetchall()
                revocations = tuple(Revocation(plan_id=plan.plan_id, revision=plan.revision, authority_epoch=epoch,
                    sequence=g["skip_sequence"], assignment_ids=tuple(sorted(a for a, group in membership.items() if group == g["id"])))
                    for g in sorted(skipped, key=lambda g: g["skip_sequence"]) if g["id"] in membership.values())
                rows = conn.execute("SELECT assignment_id,committed_at,readiness_sequence FROM execution_commits "
                                    "WHERE player_id=%s AND authority_epoch=%s AND revision=%s AND valid",
                                    (player_id, epoch, plan.revision)).fetchall()
                if rows:
                    # Groups can have been granted against different Player snapshots.
                    commits = tuple(Commit(plan_id=plan.plan_id, revision=plan.revision, authority_epoch=epoch,
                        readiness_sequence=sequence,
                        assignment_ids=tuple(sorted(r["assignment_id"] for r in rows if r["readiness_sequence"] == sequence)),
                        committed_at=max(r["committed_at"] for r in rows if r["readiness_sequence"] == sequence))
                        for sequence in sorted({r["readiness_sequence"] for r in rows}))
            return {"configuration": config, "plan": plan, "commits": commits,
                    "revocations": revocations, "server_time": self.clock.utc()}

    def readiness(self, player_id: str, report: Readiness) -> bool:
        now = self.clock.utc()
        with self._transaction() as conn:
            configurations = {p["id"]: self._configuration(conn, p["id"], p["authority_epoch"])
                              for p in self._players(conn)}
            config = configurations.get(player_id)
            if config is None or config.authority_epoch != report.authority_epoch:
                raise CoordinationError("stale_authority", 403)
            row = conn.execute("SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
                               "AND revision=%s AND plan_id=%s AND valid_until>%s",
                               (player_id, report.authority_epoch, report.revision, report.plan_id, now)).fetchone()
            if row is None:
                raise CoordinationError("unknown_offer")
            old = conn.execute("SELECT sequence FROM player_feedback WHERE player_id=%s AND authority_epoch=%s",
                               (player_id, report.authority_epoch)).fetchone()
            if old and report.sequence <= old["sequence"]:
                return False
            if report.observed_at > now + self.limits.max_uncertainty:
                raise CoordinationError("future_readiness")
            plan = Plan.model_validate(row["manifest"])
            layers = {layer.assignment_id: layer for layer in plan.layers}
            reported = set(report.secured) | set(report.prepared) | {f.assignment_id for f in report.failures}
            if not reported <= layers.keys():
                raise CoordinationError("unknown_assignment")
            if any(not self._authorized(layers[a], config) for a in reported):
                raise CoordinationError("stale_binding", 403)
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
            for assignment in report.secured:
                layer = layers[assignment]
                if layer.end <= now:
                    continue
                old_lock = conn.execute("SELECT layer FROM assignment_locks WHERE player_id=%s "
                                        "AND authority_epoch=%s AND assignment_id=%s",
                                        (player_id, report.authority_epoch, assignment)).fetchone()
                if old_lock and not same_execution(Layer.model_validate(old_lock["layer"]), layer):
                    raise CoordinationError("content_lock_conflict")
                conn.execute("INSERT INTO assignment_locks VALUES(%s,%s,%s,%s,%s,%s) "
                             "ON CONFLICT(player_id,authority_epoch,assignment_id) DO NOTHING",
                             (player_id, report.authority_epoch, assignment,
                              Jsonb(layer.model_dump(mode="json")), now, layer.end))
                if layer.variant:
                    conn.execute("INSERT INTO media_references VALUES(%s,%s,%s) "
                                 "ON CONFLICT(owner,digest) DO UPDATE SET expires_at=EXCLUDED.expires_at",
                                 (f"secured:{player_id}:{report.authority_epoch}:{assignment}",
                                  layer.variant.sha256, layer.end))
            conn.execute("INSERT INTO player_feedback VALUES(%s,%s,%s,%s,%s) "
                         "ON CONFLICT(player_id,authority_epoch) DO UPDATE SET sequence=EXCLUDED.sequence,"
                         "received_at=EXCLUDED.received_at,readiness=EXCLUDED.readiness",
                         (player_id, report.authority_epoch, report.sequence, now,
                          Jsonb(report.model_dump(mode="json"))))
            failed = {f.assignment_id: f.code for f in report.failures}
            # Loss is local first on the Player; this persists central invalidation.
            current = self._current_plan(conn, player_id, report.authority_epoch)
            if current and current.revision == report.revision and current.bindings == config.bindings:
                for layer in current.layers:
                    if layer.assignment_id not in report.prepared or not report.capacity_ok or \
                            report.clock_uncertainty > self.limits.max_uncertainty:
                        groups = conn.execute("SELECT DISTINCT g.id FROM execution_commits c "
                                              "JOIN coordination_groups g ON g.id=c.group_id "
                                              "WHERE c.player_id=%s AND c.authority_epoch=%s "
                                              "AND c.assignment_id=%s AND c.valid AND g.starts_at>%s",
                                              (player_id, report.authority_epoch, layer.assignment_id, now)).fetchall()
                        for group in groups:
                            self._skip_group(conn, group["id"], "readiness_lost")
                        conn.execute("UPDATE execution_commits SET valid=FALSE WHERE player_id=%s "
                                     "AND authority_epoch=%s AND assignment_id=%s",
                                     (player_id, report.authority_epoch, layer.assignment_id))
            for assignment, code in failed.items():
                self._event(conn, "readiness_lost", player_id, assignment, {"code": code})
            self._commit_due(conn, configurations, now)
            return True

    def _commit_due(self, conn, configurations, now):
        plans = {p: self._current_plan(conn, p, c.authority_epoch) for p, c in configurations.items()}
        plan_groups = {p: self._plan_groups(conn, plan) for p, plan in plans.items() if plan}
        feedback = {}
        for row in conn.execute("SELECT * FROM player_feedback").fetchall():
            config = configurations.get(row["player_id"])
            if config and config.authority_epoch == row["authority_epoch"]:
                feedback[row["player_id"]] = (row, Readiness.model_validate(row["readiness"]))
        for group in conn.execute("SELECT * FROM coordination_groups WHERE status!='skipped' "
                                  "AND valid_until>%s AND starts_at<=%s ORDER BY starts_at,id",
                                  (now, now + self.limits.prepare_seconds)).fetchall():
            if group["status"] == "pending" and now > group["deadline"]:
                self._skip_group(conn, group["id"], "not_ready")
                continue
            ready, participants = True, []
            for assignment, owner in group["members"].items():
                if group["status"] == "committed" and group["member_ends"].get(assignment, group["valid_until"]) <= now:
                    continue
                if owner is None:
                    ready = False
                    break
                player, epoch, output, generation = owner
                plan, state = plans.get(player), feedback.get(player)
                layer = next((a for a in plan.layers if a.assignment_id == assignment), None) if plan else None
                if (not plan or not state or not layer or not self._authorized(layer, configurations[player])
                    or plan.bindings != configurations[player].bindings or plan.valid_until <= now
                    or plan_groups[player].get(assignment) != group["id"]):
                    ready = False
                    break
                row, report = state
                if (plan.authority_epoch != epoch or plan.revision != report.revision or
                    plan.plan_id != report.plan_id or assignment not in report.prepared or
                    not report.capacity_ok or report.clock_uncertainty > self.limits.max_uncertainty or
                    now - report.observed_at > self.limits.readiness_seconds or
                    now - row["received_at"] > self.limits.readiness_seconds or
                    layer.output_id != output or layer.binding_generation != generation):
                    ready = False
                    break
                participants.append((plan, layer, report.sequence))
            if ready and participants:
                for plan, layer, sequence in participants:
                    conn.execute("INSERT INTO execution_commits VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s) "
                                 "ON CONFLICT(player_id,authority_epoch,revision,assignment_id) "
                                 "DO UPDATE SET valid=TRUE,committed_at=EXCLUDED.committed_at,"
                                 "readiness_sequence=EXCLUDED.readiness_sequence",
                                 (plan.player_id, plan.authority_epoch, plan.revision,
                                  layer.assignment_id, group["id"], now, sequence))
                conn.execute("UPDATE coordination_groups SET status='committed' WHERE id=%s", (group["id"],))
            elif group["status"] == "pending" and now >= group["deadline"]:
                self._skip_group(conn, group["id"], "not_ready")

    def observe(self, player_id: str, observation: Observation):
        with self._transaction() as conn:
            self._players(conn)
            config = self._configuration(conn, player_id, observation.authority_epoch)
            plan = self._current_plan(conn, player_id, observation.authority_epoch)
            if not plan or (plan.plan_id, plan.revision) != (observation.plan_id, observation.revision):
                raise CoordinationError("stale_observation")
            layer = next((a for a in plan.layers if a.assignment_id == observation.assignment_id), None)
            if not layer or not self._authorized(layer, config):
                raise CoordinationError("stale_binding", 403)
            self._event(conn, "observation", player_id, layer.assignment_id,
                        observation.model_dump(mode="json"))
