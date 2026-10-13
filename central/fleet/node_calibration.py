"""Live CalibrationTrial application owner; exact presentation precedes atomic Save.

Trials are operational, leased and separate from Registry's persistent calibration.
The lease has two deadlines: an idle one (`inactivity_seconds`) that an edit or a
`keepalive` slides forward, and a hard one (`hard_seconds`) fixed at begin, which the
Node never lets a trial extend (appliance/display_host/weston.py, native/shell.c refuse
one over 120 s; 110 s leaves room for a step in Central's clock). An open console page
sends `keepalive` while it shows the Frame's Position or Picture tab, so a trial ends
within the idle window once the page closes.

A trial carries the operator's draft and the saved calibration revision the draft was made
from (its base, `calibration.revision`). `begin` may start from a draft and `renew` hands an
active trial over to the next one before its hard deadline, carrying the draft, in ONE
transaction: the old trial ends and the new one (the next generation) is the Frame's active
trial at once, so the Node is handed the draft on its next exchange, with no gap that shows
the saved calibration and no gap another console could begin in. Both refuse a draft whose
base is not the saved revision (`trial_baseline_revision_changed`), as `edit` does, so a
save made elsewhere is never overwritten across trials.

The print look adds the Frame's mat (contracts.models.Mat: its depth and lights) to the trial
beside the calibration: a trial begins at the saved mat (frames.mat) unless given a draft one,
`edit` and `renew` carry the whole draft mat (`trial_mat_required` without it,
`trial_mat_invalid` for one the contract refuses), the candidate hash covers it, so the
Player's presented tag proves the mat too, and Save writes it to the binding with the
calibration. The mat never changes the Frame output.
The only persistence write crosses Registry's in-transaction CAS port. Failed Save
rolls back its row lock and all writes; it cannot strand a durable frozen trial.

The lease and the expected sequence are the Live model's shared rules (central/live_session.py),
which the Display's picture session follows too; this module maps them to its own error codes.
"""

from __future__ import annotations

from contextlib import suppress
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from central.fleet.node_sessions import NodeControlError
from central.fleet.position_history import (
    TrialHanded,
    position_values,
    restore_refusal,
    restored_draft,
)
from central.infra.position_history_store import (
    frame_made_for_in,
    position_entry_in,
    record_sent_in,
)
from central.live_session import Lease, StaleSequence, next_sequence, touched
from central.transaction_locks import acquire_runtime_locks
from contracts.models import Calibration, Mat
from contracts.node_calibration import (
    MAX_MAT_BYTES,
    TrialCandidate,
    candidate_hash,
    canonical,
    encode_trial,
)
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

    def begin(self, frame_id: str, calibration: dict | None = None, mat: dict | None = None) -> dict:
        """A new trial for the Frame, at the saved calibration or at `calibration`, the
        operator's draft whose `revision` is the saved revision it was made from; and at the
        saved mat (frames.mat) or at `mat`, the operator's draft mat."""
        draft = None if calibration is None else Calibration.model_validate(calibration)
        draft_mat = None if mat is None else _mat(mat)
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
            return self._open(conn, frame_id, context, draft, draft_mat, now, session_id=None, restored_from=None)

    def _open(self, conn, frame_id: str, context, draft: Calibration | None, draft_mat: Mat | None,
              now: float, *, session_id: UUID | None, restored_from: UUID | None) -> dict:
        """Insert the Frame's next-generation active trial (the caller holds the locks and has
        ended any active one): at the saved calibration, or at `draft` if its base is it; at the
        saved mat, or at `draft_mat`. Its Position history session is `session_id` (a renew's
        predecessor's), or its own id when None (a begin); `restored_from` the entry its draft was
        last restored from."""
        saved = context.binding.calibration
        if draft is not None and draft.revision != saved.revision:
            raise NodeControlError("trial_baseline_revision_changed", 409)
        calibration = saved if draft is None else draft
        mat = context.binding.mat if draft_mat is None else draft_mat
        generation = conn.execute(
            "SELECT COALESCE(max(generation),0)+1 AS n "
            "FROM node_calibration_trials WHERE frame_id=%s",
            (frame_id,),
        ).fetchone()["n"]
        trial_id = uuid4()
        session_id = trial_id if session_id is None else session_id
        baseline = context.request.admitted
        payload = canonical(calibration.model_dump(mode="json"))
        sha = candidate_hash(trial_id, generation, 1, baseline, payload, _mat_json(mat))
        row = conn.execute(
            "INSERT INTO node_calibration_trials(trial_id,frame_id,generation,player_id,"
            "authority_epoch,producer_id,baseline,calibration_revision,sequence,calibration,mat,candidate_sha256,"
            "state,created_at,touched_at,expires_at,hard_expires_at,session_id,restored_from) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s) RETURNING *",
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
                Jsonb(mat.model_dump(mode="json")),
                sha,
                now,
                now,
                now + self.inactivity_seconds,
                now + self.hard_seconds,
                session_id,
                restored_from,
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
        mat: dict | None = None,
    ) -> dict:
        """One operation on the trial; `edit` and `renew` carry the whole draft: `calibration`
        and `mat`."""
        if operation not in ("edit", "save", "end", "status", "keepalive", "renew"):
            raise NodeControlError("trial_operation_invalid", 422)
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise NodeControlError("trial_sequence_invalid", 422)
        proposed = (
            Calibration.model_validate(calibration) if operation in ("edit", "renew") else None
        )
        proposed_mat = None
        if operation in ("edit", "renew"):
            if mat is None:
                raise NodeControlError("trial_mat_required", 422)
            proposed_mat = _mat(mat)
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
                if not _lease(row).live(now):
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
                    (now, self._touched(row, now), trial_id),
                ).fetchone())
            try:
                sequence = next_sequence(row["sequence"], expected_sequence)
            except StaleSequence:
                raise NodeControlError("trial_sequence_conflict", 409) from None
            if operation == "renew":
                # The handover: this trial ends and the next generation begins at the draft, in
                # this one transaction (the module docstring).
                if proposed.revision != row["calibration_revision"]:
                    raise NodeControlError("trial_baseline_revision_changed", 409)
                conn.execute(
                    "UPDATE node_calibration_trials SET state='ended' WHERE trial_id=%s", (trial_id,)
                )
                return self._open(conn, frame_id, context, proposed, proposed_mat, now,
                                  session_id=row["session_id"], restored_from=row["restored_from"])
            if operation == "end":
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state='ended' WHERE trial_id=%s RETURNING *",
                    (trial_id,),
                ).fetchone()
            elif operation == "edit":
                if proposed.revision != row["calibration_revision"]:
                    raise NodeControlError("trial_baseline_revision_changed", 409)
                row = self._edited_in(conn, row, sequence, proposed, proposed_mat, now,
                                      restored_from=row["restored_from"])
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
                    mat=Mat.model_validate(row["mat"]),
                    session_id=row["session_id"],
                    restored_from=row["restored_from"],
                )
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state='saved',saved_calibration=%s,saved_mat=%s "
                    "WHERE trial_id=%s RETURNING *",
                    (Jsonb(committed.model_dump(mode="json")), Jsonb(row["mat"]), trial_id),
                ).fetchone()
            return self._view(row)

    def restore(self, frame_id: str, trial_id: UUID, *, entry_id: UUID, expected_sequence: int) -> dict:
        """Load a Position history entry into the active trial's draft (configuration snapshots,
        "View and restore"): an `edit` whose calibration and mat are
        `position_history.restored_draft(entry, base_revision=<the trial's calibration_revision>)`,
        recording `restored_from`; Done still keeps it. Answers the trial's view, as `operate`
        does (the console replaces its draft with the answer's calibration and mat).
        NodeControlError: trial_sequence_invalid 422 (not an int >= 1), trial_unknown 404,
        trial_ended 410 (the trial is not active, or its lease ran out or was invalidated here),
        unknown_entry 404 (no such entry, or trimmed), then `position_history.restore_refusal`
        against the Frame's profile and the Display seen on its port now (other_frame,
        made_for_other_profile, made_for_other_display, each 409), then trial_sequence_conflict 409."""
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise NodeControlError("trial_sequence_invalid", 422)
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            # Find and lock the current operational scope before the Trial row, as `operate` does.
            context = None
            with suppress(NodeControlError):
                context = self.display.current_frame_in(conn, frame_id)
            row = conn.execute(
                "SELECT * FROM node_calibration_trials WHERE trial_id=%s AND frame_id=%s FOR UPDATE",
                (trial_id, frame_id),
            ).fetchone()
            if row is None:
                raise NodeControlError("trial_unknown", 404)
            now = self.sessions.clock.utc()
            if (row["state"] != "active" or not _lease(row).live(now) or context is None
                    or not self._same(row, context.producer_id, context.request.admitted)):
                raise NodeControlError("trial_ended", 410)
            entry = position_entry_in(conn, entry_id)
            if entry is None:
                raise NodeControlError("unknown_entry", 404)
            made_for = frame_made_for_in(conn, frame_id)
            if made_for is None:
                raise NodeControlError("trial_unknown", 404)
            refusal = restore_refusal(entry, frame_id=frame_id, profile_px=made_for.profile_px,
                                      seen_display_id=made_for.display_id)
            if refusal is not None:
                raise NodeControlError(refusal.value, 409)
            try:
                sequence = next_sequence(row["sequence"], expected_sequence)
            except StaleSequence:
                raise NodeControlError("trial_sequence_conflict", 409) from None
            calibration, mat = restored_draft(entry, base_revision=row["calibration_revision"])
            return self._view(self._edited_in(conn, row, sequence, calibration, mat, now, restored_from=entry_id))

    def _edited_in(self, conn, row, sequence: int, calibration: Calibration, mat: Mat, now: float, *,
                   restored_from: UUID | None):
        """The active trial `row` at a new draft (an edit's or a Restore's): its candidate rehashed at
        `sequence`, its presentation cleared, its idle deadline slid, and the entry its draft was last
        restored from. The updated row."""
        sha = candidate_hash(
            row["trial_id"],
            row["generation"],
            sequence,
            surface_from(row["baseline"]),
            canonical(calibration.model_dump(mode="json")),
            _mat_json(mat),
        )
        return conn.execute(
            "UPDATE node_calibration_trials SET sequence=%s,calibration=%s,mat=%s,candidate_sha256=%s,"
            "touched_at=%s,expires_at=%s,presented_sequence=NULL,presented_sha256=NULL,"
            "presentation_request_id=NULL,presented_at=NULL,restored_from=%s WHERE trial_id=%s RETURNING *",
            (
                sequence,
                Jsonb(calibration.model_dump(mode="json")),
                Jsonb(mat.model_dump(mode="json")),
                sha,
                now,
                self._touched(row, now),
                restored_from,
                row["trial_id"],
            ),
        ).fetchone()

    def _touched(self, row, now: float) -> float:
        """The trial's idle deadline after an edit or keepalive at `now` (never past its hard one)."""
        return touched(_lease(row), now, idle_seconds=self.inactivity_seconds).idle_until

    @staticmethod
    def _same(row, producer_id, surface) -> bool:
        return (
            row["producer_id"] == producer_id
            and surface is not None
            and surface_from(row["baseline"]) == surface
        )

    def deliver_in(self, conn, principal, request, link, binding, now: float) -> str | None:
        """The trial this exchange hands the Node for the bound Frame (None: none, and the Node
        shows the saved Position), recorded as Position history when what it hands changed
        (configuration snapshots: `position_history_store.record_sent_in`)."""
        if link is None or binding is None:
            return None
        saved = position_values(binding.calibration, binding.mat)
        row = conn.execute(
            "SELECT * FROM node_calibration_trials WHERE frame_id=%s AND state='active' FOR UPDATE",
            (binding.frame_id,),
        ).fetchone()
        if row is None:
            record_sent_in(conn, binding.frame_id, trial=None, saved=saved)
            return None
        live = _lease(row).live(now)
        if (
            not live
            or not self._same(row, principal.producer_id, request.admitted)
            or row["authority_epoch"] != link.authority_epoch
        ):
            conn.execute(
                "UPDATE node_calibration_trials SET state=%s WHERE trial_id=%s",
                ("invalidated" if live else "expired", row["trial_id"]),
            )
            record_sent_in(conn, binding.frame_id, trial=None, saved=saved)
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
            mat_json=canonical(row["mat"]),
        )
        handed = TrialHanded(
            position_values(Calibration.model_validate(row["calibration"]), Mat.model_validate(row["mat"])),
            row["session_id"] or row["trial_id"], row["restored_from"])
        record_sent_in(conn, binding.frame_id, trial=handed, saved=saved)
        return encode_trial(candidate)


def _mat(value: object) -> Mat:
    """The operator's draft mat; NodeControlError("trial_mat_invalid", 422) for one the contract
    refuses (contracts.models.Mat's ranges, or past the trial's MAX_MAT_BYTES)."""
    try:
        mat = Mat.model_validate(value)
    except ValidationError:
        raise NodeControlError("trial_mat_invalid", 422) from None
    if len(_mat_json(mat).encode()) > MAX_MAT_BYTES:
        raise NodeControlError("trial_mat_invalid", 422)
    return mat


def _mat_json(mat: Mat) -> str:
    """The mat as the trial carries and hashes it (contracts.node_calibration `mat_json`)."""
    return canonical(mat.model_dump(mode="json"))


def _lease(row) -> Lease:
    """A trial row's lease: its idle deadline (`expires_at`) and its hard one."""
    return Lease(row["expires_at"], row["hard_expires_at"])
