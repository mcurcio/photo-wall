"""Durable Runtime admission fence for a planned Player withdrawal.

This boundary does not authorize a device command. A caller must separately
establish the accepted command trust, interruption policy and local handoff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from psycopg.types.json import Jsonb

from central.registry import RegistryError
from central.transaction_locks import acquire_runtime_locks, holds_runtime_locks_in

if TYPE_CHECKING:
    from central.coordination import Coordinator


@dataclass(frozen=True)
class DrainOutcome:
    status: Literal[
        "prepared", "stop_committed", "aborted",
        "already_prepared", "already_committed", "already_aborted",
    ]
    player_id: str
    attempt_id: str
    boot_id: str
    authority_epoch: int
    snapshot: dict


def control_fence_in(conn, player_id: str) -> dict | None:
    """Salt the v2 challenge with the drain cut even when visible state is unchanged."""
    row = conn.execute(
        "SELECT attempt_id,phase,prepared_at FROM active_equipment_drains "
        "WHERE player_id=%s", (player_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def fenced_players_in(conn) -> frozenset[str]:
    """Read the durable fence inside a Coordination-locked transaction."""
    return frozenset(row["player_id"] for row in conn.execute(
        "SELECT player_id FROM active_equipment_drains"
    ).fetchall())


def require_unfenced_player_in(conn, player_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM active_equipment_drains WHERE player_id=%s", (player_id,),
    ).fetchone():
        raise RegistryError("equipment_draining")


def require_unfenced_frame_in(conn, frame_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM bindings b JOIN active_equipment_drains d ON d.player_id=b.player_id "
        "WHERE b.frame_id=%s", (frame_id,),
    ).fetchone():
        raise RegistryError("equipment_draining")


class EquipmentDrain:
    """Prepared and committed fences share Coordinator's transaction/lock order."""

    ABORT_MARGIN_SECONDS = 2.0

    def __init__(self, coordinator: Coordinator):
        self.coordinator = coordinator

    @staticmethod
    def _affected_active_runs(runtime, frame_ids: set[str], now: float) -> tuple[str, ...]:
        """Project due Programs as well as already persisted Runs at the lock cut."""
        targets = {f"frame:{frame_id}" for frame_id in frame_ids}
        return tuple(sorted(
            run.run_id for run in runtime.project(now).runs
            if run.ended_at is None and not run.participants.isdisjoint(targets)
        ))

    def prepare_unbound(self, player_id: str, attempt_id: str, boot_id: str,
                        authority_epoch: int, *, authorization_expires_at: float) -> DrainOutcome:
        """Prepare only a Player with no bound Output at the serialized cut.

        This is the sole drain preparation eligible for a future unbound canary
        command issuer. It creates no command or stop permit itself.
        """
        with self.coordinator._transaction() as conn:
            acquire_runtime_locks(conn)
            return self.prepare_unbound_in(
                conn, player_id, attempt_id, boot_id, authority_epoch,
                authorization_expires_at=authorization_expires_at,
            )

    def prepare_unbound_in(self, conn, player_id: str, attempt_id: str, boot_id: str,
                           authority_epoch: int, *,
                           authorization_expires_at: float) -> DrainOutcome:
        """Prepare in the caller's transaction; no nested commit or stop permit.

        The caller acquires Coordination then Runtime before later fleet or
        equipment row locks. Missing locks fail before reading Runtime.
        """
        return self._prepare_idle_in(
            conn, player_id, attempt_id, boot_id, authority_epoch,
            authorization_expires_at=authorization_expires_at, require_unbound=True,
        )

    def prepare_idle(self, player_id: str, attempt_id: str, boot_id: str,
                     authority_epoch: int, *, authorization_expires_at: float) -> DrainOutcome:
        """Refuse bound preparation until D16 decides due-Program admission.

        A prepared barrier could otherwise outlive the authorization because
        a scheduled Program admitted during the drain can block its abort.
        The unbound canary has a separate, still-available entry point.
        """
        raise RegistryError("bound_drain_policy_unselected")

    def _prepare_idle_in(self, conn, player_id: str, attempt_id: str, boot_id: str,
                         authority_epoch: int, *, authorization_expires_at: float,
                         require_unbound: bool) -> DrainOutcome:
        if not holds_runtime_locks_in(conn):
            raise RegistryError("runtime_snapshot_required", 500)
        if (not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 160
                or not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 160
                or type(authority_epoch) is not int or authority_epoch < 1
                or type(authorization_expires_at) not in (int, float)
                or not math.isfinite(authorization_expires_at)):
            raise RegistryError("invalid_drain_request", 422)
        player = conn.execute(
            "SELECT authority_epoch,retired_at FROM players WHERE id=%s FOR UPDATE",
            (player_id,),
        ).fetchone()
        if not player or player["retired_at"] is not None:
            raise RegistryError("unknown_or_retired_player", 404)
        if player["authority_epoch"] != authority_epoch:
            raise RegistryError("stale_authority", 403)
        existing = conn.execute(
            "SELECT * FROM equipment_drains WHERE player_id=%s", (player_id,),
        ).fetchone()
        if existing:
            same_attempt = (
                existing["attempt_id"], existing["boot_id"], existing["authority_epoch"]
            ) == (attempt_id, boot_id, authority_epoch)
            if existing["phase"] == "aborted":
                if same_attempt:
                    return self._outcome(existing, "already_aborted")
            elif not same_attempt:
                raise RegistryError("equipment_drain_conflict")
            else:
                if require_unbound:
                    self._verify_unbound_snapshot_in(conn, player_id, existing["snapshot"])
                return self._outcome(existing, "already_committed" if
                                     existing["phase"] == "stop_committed" else
                                     "already_prepared")
        outputs = conn.execute(
            "SELECT output_id,observation FROM outputs WHERE player_id=%s "
            "ORDER BY output_id FOR UPDATE",
            (player_id,),
        ).fetchall()
        if not outputs:
            raise RegistryError("no_observed_outputs")
        bindings = conn.execute(
            "SELECT b.output_id,b.frame_id,f.generation,f.configuration_revision,"
            "f.calibration,f.preview FROM bindings b JOIN frames f ON f.id=b.frame_id "
            "WHERE b.player_id=%s ORDER BY b.output_id FOR UPDATE OF b,f", (player_id,),
        ).fetchall()
        if require_unbound and bindings:
            raise RegistryError("unbound_drain_requires_unbound_outputs")
        cut_now = self.coordinator.clock.utc()
        if not cut_now < authorization_expires_at <= cut_now + 600:
            raise RegistryError("invalid_drain_request", 422)
        frame_ids = {row["frame_id"] for row in bindings}
        # Settle all due Runtime work at the same post-lock instant used to
        # decide idleness, before making the durable admission barrier visible.
        with self.coordinator.runtime.edit(conn) as runtime:
            runtime.advance(cut_now)
            affected = self._affected_active_runs(runtime, frame_ids, cut_now)
            if affected:
                raise RegistryError("active_run_requires_interruption_policy", details={
                    "run_ids": list(affected),
                })
        control = conn.execute(
            "SELECT authority_epoch,issued_sequence FROM player_control_sessions "
            "WHERE player_id=%s", (player_id,),
        ).fetchone()
        snapshot = {
            "outputs": [
                {
                    "output_id": row["output_id"],
                    "frame_id": bound["frame_id"] if bound else None,
                    "binding_generation": bound["generation"] if bound else None,
                    "configuration_revision": (
                        bound["configuration_revision"] + int(bound["preview"] is not None)
                        if bound else None
                    ),
                    "calibration_revision": bound["calibration"]["revision"] if bound else None,
                    **({"observation": row["observation"]} if require_unbound else {}),
                }
                for row in outputs
                for bound in [next((b for b in bindings if b["output_id"] == row["output_id"]), None)]
            ],
            "run_participants": [],
            "actuators": [],
            "control_floor_sequence": (
                control["issued_sequence"] if control and
                control["authority_epoch"] == authority_epoch else None
            ),
        }
        if require_unbound:
            snapshot["admission_scope"] = "unbound_canary"
        # End visual cues before the old app can be withdrawn. The Frame row
        # locks above serialize this with configuration and calibration reads.
        conn.execute("DELETE FROM player_output_identification WHERE player_id=%s", (player_id,))
        conn.execute(
            "UPDATE frames SET preview=NULL,preview_expires=NULL,"
            "configuration_revision=configuration_revision+1 "
            "WHERE id=ANY(%s) AND preview IS NOT NULL", (list(frame_ids),),
        )
        if existing:
            conn.execute(
                "UPDATE equipment_drains SET attempt_id=%s,boot_id=%s,authority_epoch=%s,"
                "phase='prepared',prepared_at=%s,authorization_expires_at=%s,"
                "stop_committed_at=NULL,aborted_at=NULL,snapshot=%s WHERE player_id=%s",
                (attempt_id, boot_id, authority_epoch, cut_now, authorization_expires_at,
                 Jsonb(snapshot), player_id),
            )
        else:
            conn.execute(
                "INSERT INTO equipment_drains(player_id,attempt_id,boot_id,authority_epoch,"
                "phase,prepared_at,authorization_expires_at,snapshot) "
                "VALUES(%s,%s,%s,%s,'prepared',%s,%s,%s)",
                (player_id, attempt_id, boot_id, authority_epoch, cut_now,
                 authorization_expires_at, Jsonb(snapshot)),
            )
        # Skipping the entire atomic group also fences other participants.
        groups = conn.execute(
            "SELECT id,members FROM coordination_groups WHERE status!='skipped'",
        ).fetchall()
        for group in groups:
            if any(owner is not None and owner[0] == player_id
                   for owner in group["members"].values()):
                self.coordinator._skip_group(conn, group["id"], "equipment_drain", cancel=True)
        return DrainOutcome("prepared", player_id, attempt_id, boot_id,
                            authority_epoch, snapshot)

    @staticmethod
    def _verify_unbound_snapshot_in(conn, player_id: str, snapshot: dict) -> None:
        """Lock the complete current Output set and compare the frozen inventory."""
        if snapshot.get("admission_scope") != "unbound_canary":
            raise RegistryError("unbound_drain_snapshot_changed")
        outputs = conn.execute(
            "SELECT output_id,observation FROM outputs WHERE player_id=%s "
            "ORDER BY output_id FOR UPDATE", (player_id,),
        ).fetchall()
        bindings = conn.execute(
            "SELECT b.output_id,b.frame_id,f.generation,f.configuration_revision,"
            "f.calibration,f.preview FROM bindings b JOIN frames f ON f.id=b.frame_id "
            "WHERE b.player_id=%s ORDER BY b.output_id FOR UPDATE OF b,f", (player_id,),
        ).fetchall()
        expected = snapshot.get("outputs")
        current = [
            {
                "output_id": output["output_id"], "frame_id": None,
                "binding_generation": None, "configuration_revision": None,
                "calibration_revision": None, "observation": output["observation"],
            }
            for output in outputs
        ]
        if not current or bindings or expected != current:
            raise RegistryError("unbound_drain_snapshot_changed")

    def commit_stop_unbound(self, player_id: str, attempt_id: str, boot_id: str,
                            authority_epoch: int) -> DrainOutcome:
        """Commit only the still-unbound prepared cut; no command is issued."""
        with self.coordinator._transaction() as conn:
            acquire_runtime_locks(conn)
            return self.commit_stop_unbound_in(
                conn, player_id, attempt_id, boot_id, authority_epoch,
            )

    def commit_stop_unbound_in(self, conn, player_id: str, attempt_id: str,
                               boot_id: str, authority_epoch: int) -> DrainOutcome:
        """Commit in the caller's transaction; no command or permit is issued.

        The caller acquires Coordination then Runtime before later fleet or
        equipment row locks. Missing locks fail before reading Runtime.
        """
        return self._commit_stop_in(
            conn, player_id, attempt_id, boot_id, authority_epoch,
            require_unbound=True,
        )

    def commit_stop(self, player_id: str, attempt_id: str, boot_id: str,
                    authority_epoch: int) -> DrainOutcome:
        """Persist the irreversible admission phase; no command is issued here."""
        return self._commit_stop(player_id, attempt_id, boot_id, authority_epoch,
                                 require_unbound=False)

    def _commit_stop(self, player_id: str, attempt_id: str, boot_id: str,
                     authority_epoch: int, *, require_unbound: bool) -> DrainOutcome:
        with self.coordinator._transaction() as conn:
            acquire_runtime_locks(conn)
            return self._commit_stop_in(
                conn, player_id, attempt_id, boot_id, authority_epoch,
                require_unbound=require_unbound,
            )

    def _commit_stop_in(self, conn, player_id: str, attempt_id: str, boot_id: str,
                        authority_epoch: int, *, require_unbound: bool) -> DrainOutcome:
        if not holds_runtime_locks_in(conn):
            raise RegistryError("runtime_snapshot_required", 500)
        runtime = self.coordinator.runtime.read_in(conn)
        row = conn.execute(
            "SELECT * FROM equipment_drains WHERE player_id=%s FOR UPDATE", (player_id,),
        ).fetchone()
        if row is None:
            raise RegistryError("unknown_drain", 404)
        if (row["attempt_id"], row["boot_id"], row["authority_epoch"]) != (
            attempt_id, boot_id, authority_epoch
        ):
            raise RegistryError("equipment_drain_conflict")
        if require_unbound and row["snapshot"].get("admission_scope") != "unbound_canary":
            raise RegistryError("unbound_drain_snapshot_changed")
        if not require_unbound and row["snapshot"].get("admission_scope") == "unbound_canary":
            raise RegistryError("unbound_drain_requires_safe_commit")
        if row["phase"] == "stop_committed":
            if require_unbound:
                current = conn.execute(
                    "SELECT authority_epoch,retired_at FROM players WHERE id=%s FOR UPDATE",
                    (player_id,),
                ).fetchone()
                if (current is None or current["retired_at"] is not None
                        or current["authority_epoch"] != authority_epoch):
                    raise RegistryError("stale_authority", 403)
                self._verify_unbound_snapshot_in(conn, player_id, row["snapshot"])
            return self._outcome(row, "already_committed")
        if row["phase"] == "aborted":
            raise RegistryError("drain_aborted")
        current = conn.execute(
            "SELECT authority_epoch,retired_at FROM players WHERE id=%s FOR UPDATE",
            (player_id,),
        ).fetchone()
        if (current is None or current["retired_at"] is not None
                or current["authority_epoch"] != authority_epoch):
            raise RegistryError("stale_authority", 403)
        cut_now = self.coordinator.clock.utc()
        if row["authorization_expires_at"] <= cut_now:
            raise RegistryError("drain_authorization_expired")
        if require_unbound:
            self._verify_unbound_snapshot_in(conn, player_id, row["snapshot"])
        frame_ids = {
            output["frame_id"] for output in row["snapshot"]["outputs"]
            if output["frame_id"] is not None
        }
        affected = self._affected_active_runs(runtime, frame_ids, cut_now)
        if affected:
            raise RegistryError("active_run_requires_interruption_policy", details={
                "run_ids": list(affected),
            })
        conn.execute(
            "UPDATE equipment_drains SET phase='stop_committed',stop_committed_at=%s "
            "WHERE player_id=%s", (cut_now, player_id),
        )
        return DrainOutcome("stop_committed", player_id, attempt_id, boot_id,
                            authority_epoch, row["snapshot"])

    def abort_prepared(self, player_id: str, attempt_id: str, boot_id: str,
                       authority_epoch: int) -> DrainOutcome:
        """Release only an expired preparation after fresh old-app control proof.

        A stopped or ambiguous attempt has no automatic release path. The next
        Coordinator advance may build fresh offers; canceled groups stay canceled.
        """
        with self.coordinator._transaction() as conn:
            acquire_runtime_locks(conn)
            row = conn.execute(
                "SELECT * FROM equipment_drains WHERE player_id=%s FOR UPDATE", (player_id,),
            ).fetchone()
            if row is None:
                raise RegistryError("unknown_drain", 404)
            if (row["attempt_id"], row["boot_id"], row["authority_epoch"]) != (
                attempt_id, boot_id, authority_epoch
            ):
                raise RegistryError("equipment_drain_conflict")
            if row["phase"] == "aborted":
                return self._outcome(row, "already_aborted")
            if row["phase"] == "stop_committed":
                raise RegistryError("stop_committed_requires_reconciliation")
            player = conn.execute(
                "SELECT authority_epoch,retired_at FROM players WHERE id=%s FOR UPDATE",
                (player_id,),
            ).fetchone()
            if (player is None or player["retired_at"] is not None
                    or player["authority_epoch"] < authority_epoch):
                raise RegistryError("stale_authority", 403)
            current_epoch = player["authority_epoch"]
            floor = row["snapshot"].get("control_floor_sequence")
            control = conn.execute(
                "SELECT authority_epoch,schema_version,status,applied_sequence,"
                "last_result,last_result_at FROM player_control_sessions WHERE player_id=%s",
                (player_id,),
            ).fetchone()
            # A re-enrollment resets the sequence. Its first v2 challenge is issued
            # after the drain and salted with this active fence, so an applied new
            # epoch proves fresh control without comparing unrelated sequence spaces.
            fresh_sequence = (control is not None and control["applied_sequence"] >
                              (floor if current_epoch == authority_epoch and floor is not None
                               else 0))
            configuration = self.coordinator._configuration(conn, player_id, current_epoch)
            plan = self.coordinator._current_plan(conn, player_id, current_epoch)
            has_commit = conn.execute(
                "SELECT 1 FROM execution_commits WHERE player_id=%s AND valid LIMIT 1",
                (player_id,),
            ).fetchone()
            cut_now = self.coordinator.clock.utc()
            if cut_now <= row["authorization_expires_at"] + self.ABORT_MARGIN_SECONDS:
                raise RegistryError("drain_authorization_open")
            if (control is None or control["authority_epoch"] != current_epoch
                    or control["status"] != "negotiated"
                    or control["schema_version"] != 2
                    or control["last_result"] != "applied"
                    or not fresh_sequence
                    or control["last_result_at"] is None
                    or control["last_result_at"] <= row["prepared_at"]):
                raise RegistryError("drain_recovery_unverified")
            if plan and (plan.valid_until <= cut_now or plan.bindings != configuration.bindings):
                raise RegistryError("drain_offers_stale")
            if has_commit:
                raise RegistryError("drain_commit_still_valid")
            frame_ids = {
                output["frame_id"] for output in row["snapshot"]["outputs"]
                if output["frame_id"] is not None
            }
            # Close Runtime's old-policy cut before the barrier disappears. A
            # later Coordinator tick may admit work under the newly open scope.
            with self.coordinator.runtime.edit(conn) as runtime:
                runtime.advance(cut_now)
                if self._affected_active_runs(runtime, frame_ids, cut_now):
                    raise RegistryError("active_run_requires_interruption_policy")
            conn.execute(
                "UPDATE equipment_drains SET phase='aborted',aborted_at=%s WHERE player_id=%s",
                (cut_now, player_id),
            )
            return DrainOutcome("aborted", player_id, attempt_id, boot_id,
                                authority_epoch, row["snapshot"])

    @staticmethod
    def _outcome(row, status) -> DrainOutcome:
        return DrainOutcome(status, row["player_id"], row["attempt_id"],
                            row["boot_id"], row["authority_epoch"], row["snapshot"])
