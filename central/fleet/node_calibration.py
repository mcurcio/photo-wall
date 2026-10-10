"""Live CalibrationTrial application owner; exact presentation precedes atomic Save.

Trials are operational, leased and separate from Registry's persistent calibration.
The lease has two deadlines: an idle one (`inactivity_seconds`) that an edit or a
`keepalive` slides forward, and a hard one (`hard_seconds`) fixed at begin, which the
Node never lets a trial extend (appliance/display_host/weston.py, native/shell.c refuse
one over 120 s; 110 s leaves room for a step in Central's clock). An open console page
sends `keepalive` while it shows the Frame's Position or Picture tab, so a trial ends
within the idle window once the page closes, and the console begins the next trial
before the hard deadline.
The only persistence write crosses Registry's in-transaction CAS port. Failed Save
rolls back its row lock and all writes; it cannot strand a durable frozen trial.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from central.fleet.node_sessions import NodeControlError
from central.transaction_locks import acquire_runtime_locks
from contracts.models import Calibration
from contracts.node_calibration import TrialCandidate, candidate_hash, canonical, encode_trial
from contracts.node_display import document, surface_from


class NodeCalibration:
    def __init__(
        self,
        sessions,
        *,
        display,
        registry,
        inactivity_seconds: float = 5,
        hard_seconds: float = 110,
    ):
        if not 1 <= inactivity_seconds <= hard_seconds <= 120:
            raise ValueError("trial_lifetime_bound")
        self.sessions, self.display, self.registry = sessions, display, registry
        self.inactivity_seconds, self.hard_seconds = inactivity_seconds, hard_seconds

    @staticmethod
    def _view(row) -> dict:
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in row.items()
            if key not in ("producer_id",)
        }

    def begin(self, frame_id: str) -> dict:
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            context = self.display.current_frame_in(conn, frame_id)
            now = self.sessions.clock.utc()
            conn.execute(
                "UPDATE node_calibration_trials SET state='expired' WHERE frame_id=%s "
                "AND state='active' AND expires_at<=%s",
                (frame_id, now),
            )
            if conn.execute(
                "SELECT 1 FROM node_calibration_trials WHERE frame_id=%s AND state='active'",
                (frame_id,),
            ).fetchone():
                raise NodeControlError("trial_already_active", 409)
            generation = conn.execute(
                "SELECT COALESCE(max(generation),0)+1 AS n "
                "FROM node_calibration_trials WHERE frame_id=%s",
                (frame_id,),
            ).fetchone()["n"]
            trial_id = uuid4()
            baseline = context.request.admitted
            calibration = context.binding.calibration
            payload = canonical(calibration.model_dump(mode="json"))
            sha = candidate_hash(trial_id, generation, 1, baseline, payload)
            row = conn.execute(
                "INSERT INTO node_calibration_trials(trial_id,frame_id,generation,player_id,"
                "authority_epoch,producer_id,baseline,calibration_revision,sequence,calibration,candidate_sha256,"
                "state,created_at,touched_at,expires_at,hard_expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,"
                "'active',%s,%s,%s,%s) RETURNING *",
                (
                    trial_id,
                    frame_id,
                    generation,
                    context.link.player_id,
                    context.link.authority_epoch,
                    context.producer_id,
                    Jsonb(document(baseline)),
                    calibration.revision,
                    Jsonb(calibration.model_dump(mode="json")),
                    sha,
                    now,
                    now,
                    now + self.inactivity_seconds,
                    now + self.hard_seconds,
                ),
            ).fetchone()
            return self._view(row)

    def operate(
        self,
        frame_id: str,
        trial_id: UUID,
        *,
        operation: str,
        expected_sequence: int,
        calibration: dict | None = None,
    ) -> dict:
        if operation not in ("edit", "save", "end", "status", "keepalive"):
            raise NodeControlError("trial_operation_invalid", 422)
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise NodeControlError("trial_sequence_invalid", 422)
        proposed = Calibration.model_validate(calibration) if operation == "edit" else None
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            terminal = conn.execute(
                "SELECT * FROM node_calibration_trials WHERE trial_id=%s AND frame_id=%s",
                (trial_id, frame_id),
            ).fetchone()
            if terminal is not None and terminal["state"] != "active":
                return self._view(
                    terminal
                )  # Exact saved/ended retry needs no successor surface authority.
            # Find and lock the current operational scope before the Trial row.
            context = None
            try:
                context = self.display.current_frame_in(conn, frame_id)
            except NodeControlError:
                if operation not in ("status", "end", "keepalive"):
                    raise
            row = conn.execute(
                "SELECT * FROM node_calibration_trials WHERE trial_id=%s AND frame_id=%s "
                "FOR UPDATE",
                (trial_id, frame_id),
            ).fetchone()
            if row is None:
                raise NodeControlError("trial_unknown", 404)
            now = self.sessions.clock.utc()
            terminal = None
            if row["state"] == "active":
                if now >= row["expires_at"]:
                    terminal = "expired"
                elif context is None or not self._same(
                    row, context.producer_id, context.request.admitted
                ):
                    terminal = "invalidated"
            if terminal:
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state=%s WHERE trial_id=%s RETURNING *",
                    (terminal, trial_id),
                ).fetchone()
            if operation == "status" or row["state"] != "active":
                return self._view(row)
            if operation == "keepalive":
                # Slides only the idle deadline, never past the hard one; the candidate, its
                # sequence and its presentation are unchanged, so Save stays as it was.
                return self._view(conn.execute(
                    "UPDATE node_calibration_trials SET touched_at=%s,expires_at=%s "
                    "WHERE trial_id=%s RETURNING *",
                    (now, min(row["hard_expires_at"], now + self.inactivity_seconds), trial_id),
                ).fetchone())
            if row["sequence"] != expected_sequence:
                raise NodeControlError("trial_sequence_conflict", 409)
            if operation == "end":
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state='ended' WHERE trial_id=%s RETURNING *",
                    (trial_id,),
                ).fetchone()
            elif operation == "edit":
                if proposed.revision != row["calibration_revision"]:
                    raise NodeControlError("trial_baseline_revision_changed", 409)
                sequence = row["sequence"] + 1
                sha = candidate_hash(
                    trial_id,
                    row["generation"],
                    sequence,
                    surface_from(row["baseline"]),
                    canonical(proposed.model_dump(mode="json")),
                )
                row = conn.execute(
                    "UPDATE node_calibration_trials SET sequence=%s,calibration=%s,candidate_sha256=%s,"
                    "touched_at=%s,expires_at=%s,presented_sequence=NULL,presented_sha256=NULL,"
                    "presentation_request_id=NULL,presented_at=NULL WHERE trial_id=%s RETURNING *",
                    (
                        sequence,
                        Jsonb(proposed.model_dump(mode="json")),
                        sha,
                        now,
                        min(row["hard_expires_at"], now + self.inactivity_seconds),
                        trial_id,
                    ),
                ).fetchone()
            elif operation == "save":
                receipt = context.request.receipt
                if (
                    row["presented_sequence"] != row["sequence"]
                    or row["presented_sha256"] != row["candidate_sha256"]
                    or receipt.frame_tag != "trial-" + row["candidate_sha256"]
                    or now - row["presented_at"] >= self.inactivity_seconds
                ):
                    raise NodeControlError("trial_latest_not_presented", 409)
                baseline = surface_from(row["baseline"])
                committed = self.registry.commit_calibration_trial_in(
                    conn,
                    frame_id=frame_id,
                    player_id=row["player_id"],
                    authority_epoch=row["authority_epoch"],
                    output_id=baseline.output.output_id,
                    binding_generation=baseline.binding_generation,
                    config_revision=baseline.config_revision,
                    calibration_revision=row["calibration_revision"],
                    calibration=Calibration.model_validate(row["calibration"]),
                )
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state='saved',saved_calibration=%s "
                    "WHERE trial_id=%s RETURNING *",
                    (Jsonb(committed.model_dump(mode="json")), trial_id),
                ).fetchone()
            return self._view(row)

    @staticmethod
    def _same(row, producer_id, surface) -> bool:
        return (
            row["producer_id"] == producer_id
            and surface is not None
            and surface_from(row["baseline"]) == surface
        )

    def deliver_in(self, conn, principal, request, link, binding, now: float) -> str | None:
        if link is None or binding is None:
            return None
        row = conn.execute(
            "SELECT * FROM node_calibration_trials WHERE frame_id=%s AND state='active' FOR UPDATE",
            (binding.frame_id,),
        ).fetchone()
        if row is None:
            return None
        if (
            now >= row["expires_at"]
            or not self._same(row, principal.producer_id, request.admitted)
            or row["authority_epoch"] != link.authority_epoch
        ):
            conn.execute(
                "UPDATE node_calibration_trials SET state=%s WHERE trial_id=%s",
                ("expired" if now >= row["expires_at"] else "invalidated", row["trial_id"]),
            )
            return None
        receipt = request.receipt
        if (
            receipt is not None
            and receipt.surface == request.admitted
            and (
                receipt.frame_tag == "trial-" + row["candidate_sha256"]
                and request.sampled_boottime_ms - receipt.sampled_boottime_ms
                < self.display.receipt_ms
            )
        ):
            conn.execute(
                "UPDATE node_calibration_trials SET presented_sequence=sequence,"
                "presented_sha256=candidate_sha256,presentation_request_id=%s,presented_at=%s "
                "WHERE trial_id=%s",
                (request.request_id, now, row["trial_id"]),
            )
        candidate = TrialCandidate(
            row["trial_id"],
            row["generation"],
            row["sequence"],
            request.admitted,
            canonical(row["calibration"]),
            row["candidate_sha256"],
            request.sampled_boottime_ms + max(1, int((row["expires_at"] - now) * 1000)),
            request.sampled_boottime_ms + max(1, int((row["hard_expires_at"] - now) * 1000)),
        )
        return encode_trial(candidate)
