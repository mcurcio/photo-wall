"""Internal, unmounted online app command and stop-permit lifecycle.

This owner joins fleet selection, Runtime withdrawal and the rollout fence in
two short database transactions. It does not authenticate an HTTP request,
fetch bytes, drive the loader OS, or authorize a bound Player. A future T1/T2
verifier must supply the principal and a certified rollout adapter must open
the gate before either transition can run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from central.coordination import Coordinator
from central.db import Database
from central.equipment_drain import EquipmentDrain
from central.fleet.attempts import AttemptService
from central.fleet.locks import lock_fleet_assets_in
from central.fleet.maintenance_requests import MaintenanceRequestStore
from central.fleet.models import FleetError
from central.fleet.policy import effective_app
from central.fleet.principal import VerifiedOsPrincipal, require_current_principal_in
from central.fleet.rollout_gate import GateAdmission, RolloutEffectGate
from central.fleet.service import FleetService
from central.transaction_locks import acquire_runtime_locks
from contracts.os_command import MAX_COMMAND_BYTES, parse_activate_app_command
from contracts.os_stop_permit import (
    MAX_STOP_PERMIT_SECONDS,
    ReadyToStop,
    StopPermit,
    canonical_bytes,
    parse_ready,
    parse_stop_permit,
)
from contracts.player_payload import FORMAT as PAYLOAD_FORMAT
from contracts.time import Clock

MAX_COMMAND_SECONDS = 600
MIN_COMMAND_SECONDS = 1


@dataclass(frozen=True, slots=True)
class IssuedCommand:
    request_id: UUID
    attempt_id: UUID
    command_id: UUID
    drain_id: UUID
    command_bytes: bytes
    command_sha256: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class IssuedStopPermit:
    attempt_id: UUID
    command_id: UUID
    drain_id: UUID
    permit_id: UUID
    permit_bytes: bytes
    permit_sha256: str
    expires_at: float


class FleetCommandLifecycle:
    """Compose existing owners without exposing an unauthenticated route."""

    def __init__(self, db: Database, clock: Clock, gate: RolloutEffectGate) -> None:
        self.db, self.clock, self.gate = db, clock, gate
        self.attempts = AttemptService(db, clock)
        self.drain = EquipmentDrain(Coordinator(db, clock))

    def dispatch_unbound(self, principal: VerifiedOsPrincipal, *, request_id: UUID,
                         player_id: str, expected_gate_generation: int) -> IssuedCommand:
        """Atomically consume one intent, freeze two roots and prepare a drain.

        The command is a staging instruction, not a stop permit. A duplicate
        request returns the exact committed bytes only while all current
        authority and deadline checks still pass.
        """
        if type(request_id) is not UUID or type(player_id) is not str:
            raise FleetError("command_request_invalid", 422)
        with self.db.transaction() as conn:
            gate = self.gate.require_open_in(
                conn, expected_generation=expected_gate_generation,
            )  # first database lock
            acquire_runtime_locks(conn)
            self._lock_player_before_session_in(conn, principal, player_id)
            session = require_current_principal_in(conn, principal, clock=self.clock)
            request = conn.execute(
                "SELECT * FROM fleet_maintenance_requests WHERE request_id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
            now = session.ensure_current(self.clock)
            self._require_request(request, principal, now=now)
            self._require_current_policy_in(conn, request)

            existing = conn.execute(
                "SELECT * FROM fleet_app_commands WHERE request_id=%s", (request_id,),
            ).fetchone()
            if existing is not None:
                attempt = conn.execute(
                    "SELECT * FROM fleet_app_attempts WHERE attempt_id=%s FOR UPDATE",
                    (existing["attempt_id"],),
                ).fetchone()
                self._require_command_in(conn, existing, attempt, request, principal,
                                         player_id, gate, now=now)
                if attempt["phase"] != "prepared":
                    raise FleetError("command_already_stop_committed")
                gate.ensure_current_in(conn)
                if session.ensure_current(self.clock) >= existing["expires_at"]:
                    raise FleetError("command_deadline_unavailable")
                return self._issued_command(existing)

            if request["status"] != "queued":
                raise FleetError("maintenance_request_not_dispatchable")
            fallback = self._current_fallback_in(conn, principal)
            attempt = self.attempts.create_queued_in(
                conn, principal, desired_revision=request["policy_revision"],
                expected_target_sha256=request["target_sha256"],
                fallback_sha256=fallback["sha256"],
            )
            if (attempt.phase != "queued" or attempt.policy_source != request["policy_source"]
                    or attempt.target_size != request["target_size"]
                    or attempt.base_abi != request["target_base_abi"]
                    or attempt.fallback_size != fallback["size"]):
                raise FleetError("command_attempt_snapshot_mismatch")
            offer = conn.execute(
                "SELECT expires_at FROM fleet_boot_offers WHERE offer_id=%s FOR SHARE",
                (principal.offer_id,),
            ).fetchone()
            if offer is None:
                raise FleetError("command_offer_unavailable")
            expires_at = min(now + MAX_COMMAND_SECONDS, principal.expires_at,
                             request["expires_at"], offer["expires_at"])
            if expires_at <= now + MIN_COMMAND_SECONDS:
                raise FleetError("command_deadline_unavailable")
            player = conn.execute(
                "SELECT authority_epoch FROM players WHERE id=%s", (player_id,),
            ).fetchone()
            command_id, drain_id = uuid4(), uuid4()
            prepared = self.drain.prepare_unbound_in(
                conn, player_id, str(attempt.attempt_id),
                str(principal.kernel_boot_id), player["authority_epoch"],
                authorization_expires_at=expires_at,
            )
            if prepared.status != "prepared":
                raise FleetError("command_drain_not_new")
            command_bytes = self._command_bytes(principal, attempt, command_id,
                                                drain_id, expires_at)
            command_sha256 = hashlib.sha256(command_bytes).hexdigest()
            changed_at = session.ensure_current(self.clock)
            if changed_at >= expires_at or changed_at >= request["expires_at"]:
                raise FleetError("command_deadline_unavailable")
            conn.execute(
                "UPDATE fleet_app_attempts SET phase='prepared',command_id=%s,"
                "drain_id=%s,updated_at=%s WHERE attempt_id=%s AND phase='queued' "
                "AND command_id IS NULL AND drain_id IS NULL",
                (command_id, drain_id, changed_at, attempt.attempt_id),
            )
            MaintenanceRequestStore.dispatch_in(
                conn, request_id, expected_revision=request["revision"],
                now=changed_at,
            )
            row = conn.execute(
                "INSERT INTO fleet_app_commands(command_id,request_id,attempt_id,drain_id,"
                "player_id,authority_epoch,device_id,device_generation,command_session_id,"
                "policy_source,desired_revision,target_sha256,target_size,base_abi,"
                "command_bytes,command_sha256,gate_generation,gate_scope_sha256,issued_at,"
                "expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s) RETURNING *",
                (command_id, request_id, attempt.attempt_id, drain_id, player_id,
                 player["authority_epoch"], principal.device_id,
                 principal.device_generation, principal.command_session_id,
                 attempt.policy_source, attempt.desired_revision, attempt.target_sha256,
                 attempt.target_size, attempt.base_abi, command_bytes, command_sha256,
                 gate.generation, gate.scope_sha256, changed_at, expires_at),
            ).fetchone()
            gate.ensure_current_in(conn)
            if session.ensure_current(self.clock) >= expires_at:
                raise FleetError("command_deadline_unavailable")
            return self._issued_command(row)

    def authorize_stop_unbound(self, principal: VerifiedOsPrincipal, ready: ReadyToStop,
                               *, expected_gate_generation: int) -> IssuedStopPermit:
        """Commit the drain and one permit only after an exact OS staging claim.

        ReadyToStop remains a claim. The OS executor must independently recheck
        its local roots, active process and capacity at the stop boundary.
        """
        if type(ready) is not ReadyToStop:
            raise FleetError("ready_to_stop_invalid", 422)
        try:
            ready_bytes = canonical_bytes(ready)
            ready = parse_ready(ready_bytes)
        except (TypeError, ValueError) as exc:
            raise FleetError("ready_to_stop_invalid", 422) from exc
        ready_sha256 = hashlib.sha256(ready_bytes).hexdigest()
        with self.db.transaction() as conn:
            gate = self.gate.require_open_in(
                conn, expected_generation=expected_gate_generation,
            )  # first database lock
            acquire_runtime_locks(conn)
            identity = conn.execute(
                "SELECT player_id FROM fleet_app_commands WHERE command_id=%s",
                (ready.command_id,),
            ).fetchone()
            if identity is None:
                raise FleetError("command_not_found", 404)
            player_id = identity["player_id"]
            self._lock_player_before_session_in(conn, principal, player_id)
            session = require_current_principal_in(conn, principal, clock=self.clock)
            now = session.ensure_current(self.clock)
            command = conn.execute(
                "SELECT * FROM fleet_app_commands WHERE command_id=%s FOR SHARE",
                (ready.command_id,),
            ).fetchone()
            request = conn.execute(
                "SELECT * FROM fleet_maintenance_requests WHERE request_id=%s FOR UPDATE",
                (command["request_id"],),
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM fleet_app_attempts WHERE attempt_id=%s FOR UPDATE",
                (command["attempt_id"],),
            ).fetchone()
            self._require_request(request, principal, now=now)
            self._require_command_in(conn, command, attempt, request, principal,
                                     player_id, gate, now=now)
            self._require_ready(ready, ready_sha256, command, attempt)
            permit = conn.execute(
                "SELECT * FROM fleet_app_stop_permits WHERE command_id=%s",
                (command["command_id"],),
            ).fetchone()
            if permit is not None:
                self._require_permit_replay(permit, ready, ready_bytes, ready_sha256,
                                            command, attempt, gate, now=now)
                # An issued permit can already be in flight. Do not require
                # post-stop Output observations to equal the old prepared cut
                # just to replay the same still-current bytes.
                drain = conn.execute(
                    "SELECT phase,attempt_id,boot_id,authority_epoch,fleet_drain_id,"
                    "snapshot FROM equipment_drains WHERE player_id=%s FOR SHARE",
                    (player_id,),
                ).fetchone()
                if (drain is None or drain["phase"] != "stop_committed"
                        or drain["attempt_id"] != str(attempt["attempt_id"])
                        or drain["boot_id"] != str(attempt["kernel_boot_id"])
                        or drain["authority_epoch"] != command["authority_epoch"]
                        or drain["fleet_drain_id"] != command["drain_id"]
                        or drain["snapshot"].get("admission_scope") != "unbound_canary"):
                    raise FleetError("stop_permit_drain_mismatch")
                gate.ensure_current_in(conn)
                if session.ensure_current(self.clock) >= permit["expires_at"]:
                    raise FleetError("stop_permit_unavailable")
                return self._issued_permit(permit)

            self._require_current_policy_in(conn, request)
            if attempt["phase"] != "prepared":
                raise FleetError("command_attempt_not_prepared")
            drain = conn.execute(
                "SELECT phase,authorization_expires_at,fleet_drain_id "
                "FROM equipment_drains WHERE player_id=%s", (player_id,),
            ).fetchone()
            if (drain is None or drain["phase"] != "prepared"
                    or drain["fleet_drain_id"] is not None):
                raise FleetError("command_drain_not_prepared")
            expires_at = min(now + MAX_STOP_PERMIT_SECONDS, command["expires_at"],
                             principal.expires_at, request["expires_at"],
                             drain["authorization_expires_at"], gate.expires_at)
            if expires_at <= now:
                raise FleetError("stop_permit_deadline_unavailable")
            outcome = self.drain.commit_stop_unbound_fleet_in(
                conn, player_id, attempt["attempt_id"],
                attempt["kernel_boot_id"], command["authority_epoch"],
                command["drain_id"],
            )
            if outcome.status != "stop_committed":
                raise FleetError("stop_permit_drain_mismatch")
            permit_id = uuid4()
            permit_doc = StopPermit(
                installation_audience=principal.installation_audience,
                device_id=principal.device_id,
                device_generation=principal.device_generation,
                kernel_boot_id=principal.kernel_boot_id,
                offer_id=principal.offer_id,
                command_session_id=principal.command_session_id,
                attempt_id=attempt["attempt_id"], command_id=command["command_id"],
                drain_id=command["drain_id"], permit_id=permit_id,
                command_sha256=command["command_sha256"],
                ready_sha256=ready_sha256, gate_generation=gate.generation,
                issued_at=now, expires_at=expires_at,
            )
            permit_bytes = canonical_bytes(permit_doc)
            permit_sha256 = hashlib.sha256(permit_bytes).hexdigest()
            conn.execute(
                "UPDATE fleet_app_attempts SET phase='stop_committed',updated_at=%s "
                "WHERE attempt_id=%s AND phase='prepared'",
                (now, attempt["attempt_id"]),
            )
            row = conn.execute(
                "INSERT INTO fleet_app_stop_permits(permit_id,command_id,attempt_id,"
                "drain_id,ready_nonce,ready_bytes,ready_sha256,permit_bytes,permit_sha256,"
                "gate_generation,gate_scope_sha256,issued_at,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (permit_id, command["command_id"], attempt["attempt_id"],
                 command["drain_id"], ready.ready_nonce, ready_bytes, ready_sha256,
                 permit_bytes, permit_sha256, gate.generation, gate.scope_sha256,
                 now, expires_at),
            ).fetchone()
            gate.ensure_current_in(conn)
            if session.ensure_current(self.clock) >= expires_at:
                raise FleetError("stop_permit_deadline_unavailable")
            return self._issued_permit(row)

    @staticmethod
    def _lock_player_before_session_in(conn, principal: VerifiedOsPrincipal,
                                       player_id: str) -> None:
        """Mirror Registry.retire: fleet → device → Player → lifecycle/session."""
        if type(principal) is not VerifiedOsPrincipal:
            raise FleetError("verifier_principal_required", 403)
        mapping = conn.execute("SELECT device_id FROM players WHERE id=%s",
                               (player_id,)).fetchone()
        if mapping is None or mapping["device_id"] != principal.device_id:
            raise FleetError("command_player_unavailable", 404)
        lock_fleet_assets_in(conn)
        device = conn.execute(
            "SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE",
            (principal.device_id,),
        ).fetchone()
        player = conn.execute(
            "SELECT device_id,authority_epoch,retired_at FROM players "
            "WHERE id=%s FOR UPDATE", (player_id,),
        ).fetchone()
        if (device is None or device["retired_at"] is not None or player is None
                or player["retired_at"] is not None
                or player["device_id"] != principal.device_id):
            raise FleetError("command_player_unavailable", 404)

    @staticmethod
    def _require_request(request, principal: VerifiedOsPrincipal, *, now: float) -> None:
        if (request is None or request["device_id"] != principal.device_id
                or request["device_generation"] != principal.device_generation):
            raise FleetError("maintenance_request_not_found", 404)
        if now >= request["expires_at"]:
            raise FleetError("maintenance_request_expired")

    @staticmethod
    def _require_current_policy_in(conn, request) -> None:
        fleet, override = FleetService._app_policy(conn, request["device_id"])
        selected = effective_app(fleet, override)
        target = selected["target"]
        if (selected["source"] != request["policy_source"]
                or selected["revision"] != request["policy_revision"]
                or target is None or target["sha256"] != request["target_sha256"]
                or target["size"] != request["target_size"]
                or target["format"] != request["target_format"]):
            raise FleetError("maintenance_policy_conflict")

    @staticmethod
    def _current_fallback_in(conn, principal: VerifiedOsPrincipal) -> dict:
        """Derive the sole active accepted reservation; no caller-picked digest."""
        rows = conn.execute(
            "SELECT a.sha256,a.size,a.base_abi,a.trust_mode,a.evidence_ref "
            "FROM fleet_generation_acceptances AS a "
            "JOIN asset_references AS ref ON ref.kind='player-payload' "
            "AND ref.owner=a.fallback_owner AND ref.identity=a.sha256 "
            "WHERE a.device_id=%s AND a.device_generation=%s AND a.kind='app' "
            "AND a.content_key=a.sha256 AND a.trust_mode IN ('t1','t2') "
            "AND ref.locator_sha256=a.sha256 AND ref.locator_size=a.size "
            "AND ref.expected_sha256=a.sha256 AND ref.expected_size=a.size "
            "ORDER BY a.sha256 FOR SHARE OF a,ref",
            (principal.device_id, principal.device_generation),
        ).fetchall()
        if len(rows) != 1:
            raise FleetError("command_current_fallback_unavailable", 503)
        return rows[0]

    @staticmethod
    def _command_bytes(principal: VerifiedOsPrincipal, attempt, command_id: UUID,
                       drain_id: UUID, expires_at: float) -> bytes:
        document = {
            "schema": 1, "kind": "activate_app",
            "installation_audience": principal.installation_audience,
            "device_id": principal.device_id,
            "device_generation": principal.device_generation,
            "kernel_boot_id": str(principal.kernel_boot_id),
            "offer_id": str(principal.offer_id),
            "command_session_id": str(principal.command_session_id),
            "attempt_id": str(attempt.attempt_id), "command_id": str(command_id),
            "desired_revision": attempt.desired_revision, "drain_id": str(drain_id),
            "target": {"format": PAYLOAD_FORMAT, "sha256": attempt.target_sha256,
                       "size": attempt.target_size, "base_abi": attempt.base_abi},
            "fallback": {"format": PAYLOAD_FORMAT,
                         "sha256": attempt.fallback_sha256,
                         "size": attempt.fallback_size, "base_abi": attempt.base_abi},
            "expires_at": expires_at,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_COMMAND_BYTES:
            raise FleetError("command_too_large")
        parse_activate_app_command(encoded)
        return encoded

    @staticmethod
    def _require_command_in(conn, command, attempt, request,
                            principal: VerifiedOsPrincipal, player_id: str,
                            gate: GateAdmission, *, now: float) -> None:
        player = conn.execute(
            "SELECT authority_epoch FROM players WHERE id=%s", (player_id,),
        ).fetchone()
        if (attempt is None or request["status"] != "dispatched"
                or player is None
                or player["authority_epoch"] != command["authority_epoch"]
                or command["player_id"] != player_id
                or command["device_id"] != principal.device_id
                or command["device_generation"] != principal.device_generation
                or command["command_session_id"] != principal.command_session_id
                or command["gate_generation"] != gate.generation
                or command["gate_scope_sha256"] != gate.scope_sha256
                or command["request_id"] != request["request_id"]
                or command["attempt_id"] != attempt["attempt_id"]
                or command["command_id"] != attempt["command_id"]
                or command["drain_id"] != attempt["drain_id"]
                or command["desired_revision"] != request["policy_revision"]
                or command["target_sha256"] != request["target_sha256"]
                or command["target_size"] != request["target_size"]
                or command["base_abi"] != request["target_base_abi"]
                or attempt["device_generation"] != principal.device_generation
                or attempt["kernel_boot_id"] != principal.kernel_boot_id
                or attempt["offer_id"] != principal.offer_id
                or attempt["installation_audience"] != principal.installation_audience
                or attempt["command_session_id"] != principal.command_session_id
                or attempt["revoked_at"] is not None
                or attempt["root_released_at"] is not None
                or command["expires_at"] <= now
                or not AttemptService._rooted_in(conn, attempt)):
            raise FleetError("command_unavailable")
        encoded = bytes(command["command_bytes"])
        parsed = parse_activate_app_command(encoded)
        if (hashlib.sha256(encoded).hexdigest() != command["command_sha256"]
                or parsed.installation_audience != principal.installation_audience
                or parsed.device_id != principal.device_id
                or parsed.device_generation != principal.device_generation
                or parsed.kernel_boot_id != principal.kernel_boot_id
                or parsed.offer_id != principal.offer_id
                or parsed.command_session_id != principal.command_session_id
                or parsed.attempt_id != attempt["attempt_id"]
                or parsed.command_id != command["command_id"]
                or parsed.desired_revision != attempt["desired_revision"]
                or parsed.drain_id != command["drain_id"]
                or parsed.target.sha256 != attempt["target_sha256"]
                or parsed.target.size != attempt["target_size"]
                or parsed.target.base_abi != attempt["base_abi"]
                or parsed.fallback.sha256 != attempt["fallback_sha256"]
                or parsed.fallback.size != attempt["fallback_size"]
                or parsed.fallback.base_abi != attempt["base_abi"]
                or parsed.expires_at != command["expires_at"]):
            raise FleetError("command_bytes_mismatch", 503)

    @staticmethod
    def _require_ready(ready: ReadyToStop, ready_sha256: str, command,
                       attempt) -> None:
        if (ready.attempt_id != attempt["attempt_id"]
                or ready.command_id != command["command_id"]
                or ready.drain_id != command["drain_id"]
                or ready.command_sha256 != command["command_sha256"]
                or ready.target_sha256 != attempt["target_sha256"]
                or ready.target_size != attempt["target_size"]
                or ready.fallback_sha256 != attempt["fallback_sha256"]
                or ready.fallback_size != attempt["fallback_size"]
                or ready.base_abi != attempt["base_abi"]
                or ready.staged_target_sha256 != attempt["target_sha256"]
                or ready.staged_fallback_sha256 != attempt["fallback_sha256"]
                or ready.selected_sha256 != attempt["fallback_sha256"]
                or not ready_sha256):
            raise FleetError("ready_to_stop_attempt_mismatch")

    @staticmethod
    def _require_permit_replay(permit, ready: ReadyToStop, ready_bytes: bytes,
                               ready_sha256: str, command, attempt,
                               gate: GateAdmission, *, now: float) -> None:
        encoded = bytes(permit["permit_bytes"])
        document = parse_stop_permit(encoded)
        if (permit["attempt_id"] != attempt["attempt_id"]
                or permit["drain_id"] != command["drain_id"]
                or permit["ready_nonce"] != ready.ready_nonce
                or bytes(permit["ready_bytes"]) != ready_bytes
                or permit["ready_sha256"] != ready_sha256
                or permit["gate_generation"] != gate.generation
                or permit["gate_scope_sha256"] != gate.scope_sha256
                or permit["expires_at"] <= now
                or attempt["phase"] != "stop_committed"
                or hashlib.sha256(encoded).hexdigest() != permit["permit_sha256"]
                or document.permit_id != permit["permit_id"]
                or document.command_id != command["command_id"]
                or document.attempt_id != attempt["attempt_id"]
                or document.drain_id != command["drain_id"]
                or document.command_sha256 != command["command_sha256"]
                or document.ready_sha256 != ready_sha256
                or document.gate_generation != gate.generation
                or document.expires_at != permit["expires_at"]):
            raise FleetError("stop_permit_unavailable")

    @staticmethod
    def _issued_command(row) -> IssuedCommand:
        return IssuedCommand(row["request_id"], row["attempt_id"],
                             row["command_id"], row["drain_id"],
                             bytes(row["command_bytes"]), row["command_sha256"],
                             row["expires_at"])

    @staticmethod
    def _issued_permit(row) -> IssuedStopPermit:
        return IssuedStopPermit(row["attempt_id"], row["command_id"],
                                row["drain_id"], row["permit_id"],
                                bytes(row["permit_bytes"]), row["permit_sha256"],
                                row["expires_at"])
