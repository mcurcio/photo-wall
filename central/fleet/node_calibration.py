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

The trial carries the Frame's lighting (contracts.frame_lighting.FrameLighting: its lights, mat
depth and paper) beside the calibration, so the console edits a Frame's lights live on the wall: a
trial begins at the saved lighting (frames.lighting) unless given a draft one, `edit` and `renew`
carry the whole draft (`TrialEdit`), every draft lighting passes the Frame-lighting check
(`FrameLightingCheck.fitted_within`, under the house's lock; FrameLightingRefused otherwise), the
candidate hash covers it, so the Player's presented tag proves the lighting too, and Save writes it
to the binding with the calibration. The lighting never changes the Frame output.

The bodies are typed (`TrialBegin`, `TrialOperation`); the routes parse them strictly.
The only persistence write crosses Registry's in-transaction CAS port. Failed Save
rolls back its row lock and all writes; it cannot strand a durable frozen trial.

The lease and the expected sequence are the Live model's shared rules (central/live_session.py),
which the Display's picture session follows too; this module maps them to its own error codes.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import Field

from central.fleet.models import TrialRefusal, TrialRefused
from central.fleet.node_sessions import NodeControlError
from central.fleet.position_history import (
    TrialHanded,
    position_values,
    restore_refusal,
    restored_draft,
)
from central.frame_lighting.ports import FrameLightingCheck
from central.infra.position_history_store import (
    frame_made_for_in,
    position_entry_in,
    record_sent_in,
)
from central.infra.transactions import PgTransaction
from central.live_session import Lease, StaleSequence, next_sequence, touched
from central.transaction_locks import acquire_runtime_locks
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration, Model
from contracts.node_calibration import (
    TrialCandidate,
    candidate_hash,
    canonical,
    encode_trial,
)
from contracts.node_display import document, surface_from


class TrialBegin(Model):
    """POST .../calibration-trials: begin at the saved calibration and lighting, or at the operator's
    drafts (a calibration's `revision` is the saved revision it was made from)."""
    calibration: Calibration | None = None
    lighting: FrameLighting | None = None


class TrialEdit(Model):
    """An `edit` (the draft now) or a `renew` (the next trial, at the draft): the whole draft."""
    operation: Literal["edit", "renew"]
    expected_sequence: int = Field(ge=1)
    calibration: Calibration
    lighting: FrameLighting


class TrialStep(Model):
    """Every other operation on the trial."""
    operation: Literal["save", "end", "status", "keepalive"]
    expected_sequence: int = Field(ge=1)


TrialOperation = Annotated[TrialEdit | TrialStep, Field(discriminator="operation")]


class NodeCalibration:
    def __init__(
        self,
        sessions,
        *,
        display,
        registry,
        lighting: FrameLightingCheck,
        inactivity_seconds: float = 5,
        hard_seconds: float = 110,
    ):
        if not 1 <= inactivity_seconds <= hard_seconds <= 120:
            raise ValueError("trial_lifetime_bound")
        self.sessions, self.display, self.registry, self.lighting = sessions, display, registry, lighting
        self.inactivity_seconds, self.hard_seconds = inactivity_seconds, hard_seconds

    @staticmethod
    def _view(row) -> dict:
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in row.items()
            if key not in ("producer_id",)
        }

    def begin(self, frame_id: str, body: TrialBegin) -> dict:
        """A new trial for the Frame, at the saved calibration or at `body.calibration`, the
        operator's draft whose `revision` is the saved revision it was made from; and at the
        saved lighting (frames.lighting) or at `body.lighting`, the operator's draft lighting."""
        draft = body.calibration
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            draft_lighting = (None if body.lighting is None
                              else self.lighting.fitted_within(PgTransaction(conn), body.lighting))
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
                raise TrialRefused(TrialRefusal.ALREADY_ACTIVE)
            return self._open(conn, frame_id, context, draft, draft_lighting, now, session_id=None,
                              restored_from=None)

    def _open(self, conn, frame_id: str, context, draft: Calibration | None, draft_lighting: FrameLighting | None,
              now: float, *, session_id: UUID | None, restored_from: UUID | None) -> dict:
        """Insert the Frame's next-generation active trial (the caller holds the locks and has
        ended any active one): at the saved calibration, or at `draft` if its base is it; at the
        saved lighting, or at `draft_lighting`. Its Position history session is `session_id` (a renew's
        predecessor's), or its own id when None (a begin); `restored_from` the entry its draft was
        last restored from."""
        saved = context.binding.calibration
        if draft is not None and draft.revision != saved.revision:
            raise TrialRefused(TrialRefusal.BASELINE_REVISION_CHANGED)
        calibration = saved if draft is None else draft
        lighting = context.binding.lighting if draft_lighting is None else draft_lighting
        generation = conn.execute(
            "SELECT COALESCE(max(generation),0)+1 AS n "
            "FROM node_calibration_trials WHERE frame_id=%s",
            (frame_id,),
        ).fetchone()["n"]
        trial_id = uuid4()
        session_id = trial_id if session_id is None else session_id
        baseline = context.request.admitted
        payload = canonical(calibration.model_dump(mode="json"))
        sha = candidate_hash(trial_id, generation, 1, baseline, payload, lighting)
        row = conn.execute(
            "INSERT INTO node_calibration_trials(trial_id,frame_id,generation,player_id,"
            "authority_epoch,producer_id,baseline,calibration_revision,sequence,calibration,lighting,candidate_sha256,"
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
                Jsonb(lighting.model_dump(mode="json")),
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

    def operate(self, frame_id: str, trial_id: UUID, body: TrialOperation) -> dict:
        """One operation on the trial; `edit` and `renew` carry the whole draft (`TrialEdit`)."""
        operation, expected_sequence = body.operation, body.expected_sequence
        proposed = body.calibration if isinstance(body, TrialEdit) else None
        with self.sessions.db.transaction() as conn:
            acquire_runtime_locks(conn)
            proposed_lighting = (self.lighting.fitted_within(PgTransaction(conn), body.lighting)
                                 if isinstance(body, TrialEdit) else None)
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
                raise TrialRefused(TrialRefusal.UNKNOWN)
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
                raise TrialRefused(TrialRefusal.SEQUENCE_CONFLICT) from None
            if operation == "renew":
                # The handover: this trial ends and the next generation begins at the draft, in
                # this one transaction (the module docstring).
                if proposed.revision != row["calibration_revision"]:
                    raise TrialRefused(TrialRefusal.BASELINE_REVISION_CHANGED)
                conn.execute(
                    "UPDATE node_calibration_trials SET state='ended' WHERE trial_id=%s", (trial_id,)
                )
                return self._open(conn, frame_id, context, proposed, proposed_lighting, now,
                                  session_id=row["session_id"], restored_from=row["restored_from"])
            if operation == "end":
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state='ended' WHERE trial_id=%s RETURNING *",
                    (trial_id,),
                ).fetchone()
            elif operation == "edit":
                if proposed.revision != row["calibration_revision"]:
                    raise TrialRefused(TrialRefusal.BASELINE_REVISION_CHANGED)
                row = self._edited_in(conn, row, sequence, proposed, proposed_lighting, now,
                                      restored_from=row["restored_from"])
            elif operation == "save":
                receipt = context.request.receipt
                if (
                    row["presented_sequence"] != row["sequence"]
                    or row["presented_sha256"] != row["candidate_sha256"]
                    or receipt.frame_tag != "trial-" + row["candidate_sha256"]
                    or now - row["presented_at"] >= self.inactivity_seconds
                ):
                    raise TrialRefused(TrialRefusal.LATEST_NOT_PRESENTED)
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
                    lighting=FrameLighting.model_validate(row["lighting"]),
                    session_id=row["session_id"],
                    restored_from=row["restored_from"],
                )
                row = conn.execute(
                    "UPDATE node_calibration_trials SET state='saved',saved_calibration=%s,saved_lighting=%s "
                    "WHERE trial_id=%s RETURNING *",
                    (Jsonb(committed.model_dump(mode="json")), Jsonb(row["lighting"]), trial_id),
                ).fetchone()
            return self._view(row)

    def restore(self, frame_id: str, trial_id: UUID, *, entry_id: UUID, expected_sequence: int) -> dict:
        """Load a Position history entry into the active trial's draft (configuration snapshots,
        "View and restore"): an `edit` whose calibration and lighting are
        `position_history.restored_draft(entry, base_revision=<the trial's calibration_revision>)`,
        recording `restored_from`; Done still keeps it. Answers the trial's view, as `operate`
        does (the console replaces its draft with the answer's calibration and lighting).
        NodeControlError: trial_sequence_invalid 422 (not an int >= 1), trial_unknown 404,
        trial_ended 410 (the trial is not active, or its lease ran out or was invalidated here),
        unknown_entry 404 (no such entry, or trimmed), then `position_history.restore_refusal`
        against the Frame's profile and the Display seen on its port now (other_frame,
        made_for_other_profile, made_for_other_display, each 409), then trial_sequence_conflict 409,
        then the entry's lighting against the house as an edit's (FrameLightingRefused)."""
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise TrialRefused(TrialRefusal.SEQUENCE_INVALID)
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
                raise TrialRefused(TrialRefusal.UNKNOWN)
            now = self.sessions.clock.utc()
            if (row["state"] != "active" or not _lease(row).live(now) or context is None
                    or not self._same(row, context.producer_id, context.request.admitted)):
                raise TrialRefused(TrialRefusal.ENDED)
            entry = position_entry_in(conn, entry_id)
            if entry is None:
                raise TrialRefused(TrialRefusal.UNKNOWN_ENTRY)
            made_for = frame_made_for_in(conn, frame_id)
            if made_for is None:
                raise TrialRefused(TrialRefusal.UNKNOWN)
            refusal = restore_refusal(entry, frame_id=frame_id, now=made_for)
            if refusal is not None:
                raise NodeControlError(refusal.value, 409)
            try:
                sequence = next_sequence(row["sequence"], expected_sequence)
            except StaleSequence:
                raise TrialRefused(TrialRefusal.SEQUENCE_CONFLICT) from None
            calibration, lighting = restored_draft(entry, base_revision=row["calibration_revision"])
            # An entry's lighting may name a room light removed or re-reported since: it goes
            # through the same check as an edit's, so Save never keeps lighting that adds nothing.
            lighting = self.lighting.fitted_within(PgTransaction(conn), lighting)
            return self._view(self._edited_in(conn, row, sequence, calibration, lighting, now,
                                              restored_from=entry_id))

    def _edited_in(self, conn, row, sequence: int, calibration: Calibration, lighting: FrameLighting,
                   now: float, *,
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
            lighting,
        )
        return conn.execute(
            "UPDATE node_calibration_trials SET sequence=%s,calibration=%s,lighting=%s,candidate_sha256=%s,"
            "touched_at=%s,expires_at=%s,presented_sequence=NULL,presented_sha256=NULL,"
            "presentation_request_id=NULL,presented_at=NULL,restored_from=%s WHERE trial_id=%s RETURNING *",
            (
                sequence,
                Jsonb(calibration.model_dump(mode="json")),
                Jsonb(lighting.model_dump(mode="json")),
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
        saved = position_values(binding.calibration, binding.lighting)
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
            and receipt.frame_tag == "trial-" + row["candidate_sha256"]
            and self.display.recent(request, receipt)
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
            lighting=FrameLighting.model_validate(row["lighting"]),
        )
        handed = TrialHanded(
            position_values(Calibration.model_validate(row["calibration"]),
                            FrameLighting.model_validate(row["lighting"])),
            row["session_id"] or row["trial_id"], row["restored_from"])
        record_sent_in(conn, binding.frame_id, trial=handed, saved=saved)
        return encode_trial(candidate)


def _lease(row) -> Lease:
    """A trial row's lease: its idle deadline (`expires_at`) and its hard one."""
    return Lease(row["expires_at"], row["hard_expires_at"])
