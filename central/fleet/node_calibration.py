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
trial at once, so the Node is handed the draft on its next document, with no gap that shows
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

On the bus (decision 0034, S4b part 3): a trial is for the Frame's binding (its Output, generation
and configuration revision: `TrialScope`), read from Central's own facts (`bound_frame_in`), never
from a display exchange. The worker's document source puts it on the Output's `trial-<port>` key
(`trial_in`, this class as the source's `TrialProjection`), its leases as durations; the Pi states
what it presents on the same key of the display line's state bucket (`TrialReportJudge`), and Save
needs that statement for the latest candidate. Every time a trial reads or writes is the database's
(`times`), the clock the document source reads, so a lease is compared with one clock. Every write
wakes the Node's document source in its transaction.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import Field

from central.actor import Actor
from central.db import Database, DatabaseTransactionClock, TransactionClock
from central.displays.ports import TrialInForce
from central.fleet.device_fence import fence_in
from central.fleet.models import TrialRefusal, TrialRefused
from central.fleet.node_bus_presence import bus_links_in
from central.fleet.node_sessions import NodeControlError
from central.fleet.position_history import (
    TrialHanded,
    position_values,
    restore_refusal,
    restored_draft,
)
from central.frame_lighting.ports import FrameLightingCheck
from central.infra.display_store import REPORT_STREAM, REPORT_SUBJECT
from central.infra.node_link_latest import latest_values_in
from central.infra.output_wakes import notify_nodes_in
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
    MAX_TRIAL_HARD_MS,
    PositionTrial,
    TrialReport,
    TrialScope,
    candidate_hash,
    canonical,
    decode_trial_report,
    trial_report_key,
)
from contracts.node_output import OUTPUT_IDS
from contracts.node_state import FRAMES_KEY, PLAYER_LINE, decode_frame_reports
from nodeapi.buffers import KV_OPERATION_HEADER
from nodeapi.pull import Read


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


@dataclass(frozen=True)
class BoundFrame:
    """The Frame's binding as its trial needs it."""
    player_id: str
    authority_epoch: int
    device_id: str
    scope: TrialScope
    calibration: Calibration          # the binding's saved Position
    lighting: FrameLighting


def bound_frame_in(conn: Any, frame_id: str) -> BoundFrame:
    """The Frame's binding as the trial needs it, refused in this order: FRAME_UNBOUND (no binding, or
    its Player retired or on no Node); the device fence (`fence_in(conn, device_id, "act")`);
    DISPLAY_UNAVAILABLE (Central's hub does not hold the Pi's fleet link); CURRENT_OUTPUT_REQUIRED (the
    Output's report does not say it is connected); ADMITTED_SURFACE_REQUIRED (the Player's `frames`
    report on its line does not show this Frame at this generation on this Output). No Node lock: no
    exchange orders against it any more."""
    row = conn.execute(
        "SELECT f.player_id, f.output_id, f.generation, f.configuration_revision, f.calibration, f.lighting, "
        "p.authority_epoch, p.device_id, o.connected FROM frames f JOIN players p ON p.id=f.player_id "
        "LEFT JOIN outputs o ON o.player_id=f.player_id AND o.output_id=f.output_id "
        "WHERE f.id=%s AND p.retired_at IS NULL", (frame_id,)).fetchone()
    if row is None or row["device_id"] is None or row["output_id"] not in OUTPUT_IDS:
        raise TrialRefused(TrialRefusal.FRAME_UNBOUND)
    device_id = row["device_id"]
    fence_in(conn, device_id, "act")
    if bus_links_in(conn, (device_id,))[device_id]["linked"] is not True:
        raise TrialRefused(TrialRefusal.DISPLAY_UNAVAILABLE)
    if row["connected"] is not True:
        raise TrialRefused(TrialRefusal.CURRENT_OUTPUT_REQUIRED)
    if not _drawn(conn, device_id, frame_id, row["generation"], row["output_id"]):
        raise TrialRefused(TrialRefusal.ADMITTED_SURFACE_REQUIRED)
    return BoundFrame(row["player_id"], row["authority_epoch"], device_id,
                      TrialScope(row["output_id"], frame_id, row["generation"], row["configuration_revision"]),
                      Calibration.model_validate(row["calibration"]), FrameLighting.model_validate(row["lighting"]))


def _drawn(conn: Any, device_id: str, frame_id: str, generation: int, output_id: str) -> bool:
    """Whether the Player's newest `frames` report on its line shows the Frame at `generation` on
    `output_id` (a report that does not decode shows none)."""
    for latest in latest_values_in(conn, PLAYER_LINE, FRAMES_KEY, device_id=device_id):
        if latest.removed:
            continue
        try:
            reports = decode_frame_reports(latest.data)
        except ValueError:
            continue
        if any(report.frame_id == frame_id and report.generation == generation and report.output_id == output_id
               for report in reports.frames):
            return True
    return False


def _scope(row) -> TrialScope:
    """A trial row's scope."""
    return TrialScope(row["output_id"], row["frame_id"], row["binding_generation"], row["configuration_revision"])


def _current(row, bound: BoundFrame | None) -> bool:
    """Whether the trial row is for the Frame's binding now."""
    return (bound is not None and _scope(row) == bound.scope
            and (row["player_id"], row["authority_epoch"]) == (bound.player_id, bound.authority_epoch))


def _wake_in(conn: Any, player_id: str) -> None:
    """Wake the document source of the trial's Node in the caller's transaction, so it rewrites the
    Output's trial document."""
    notify_nodes_in(conn, "SELECT device_id FROM players WHERE id=%s AND device_id IS NOT NULL", (player_id,))


class NodeCalibration:
    """The Position trial's operations (the routes) and its projection on the display line (the
    worker's document source: `TrialProjection`)."""

    def __init__(
        self,
        db: Database,
        *,
        registry,
        lighting: FrameLightingCheck,
        times: TransactionClock = DatabaseTransactionClock(),
        inactivity_seconds: float = 5,
        hard_seconds: float = 110,
    ):
        if not 1 <= inactivity_seconds <= hard_seconds <= MAX_TRIAL_HARD_MS / 1000:
            raise ValueError("trial_lifetime_bound")
        self.db, self.registry, self.lighting, self.times = db, registry, lighting, times
        self.inactivity_seconds, self.hard_seconds = inactivity_seconds, hard_seconds

    @staticmethod
    def _view(row) -> dict:
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in row.items()
            if key not in ("created_by", "changed_at", "changed_by")
        }

    def begin(self, frame_id: str, body: TrialBegin, *, by: Actor) -> dict:
        """A new trial for the Frame, at the saved calibration or at `body.calibration`, the
        operator's draft whose `revision` is the saved revision it was made from; and at the
        saved lighting (frames.lighting) or at `body.lighting`, the operator's draft lighting."""
        draft = body.calibration
        with self.db.transaction() as conn:
            acquire_runtime_locks(conn)
            draft_lighting = (None if body.lighting is None
                              else self.lighting.fitted_within(PgTransaction(conn), body.lighting))
            bound = bound_frame_in(conn, frame_id)
            now = self.times.now_in(conn)
            active = conn.execute(
                "SELECT * FROM frame_trials WHERE frame_id=%s AND state='active' FOR UPDATE", (frame_id,)).fetchone()
            if active is not None:
                terminal = self._terminal(active, bound, now)
                if terminal is None:
                    raise TrialRefused(TrialRefusal.ALREADY_ACTIVE)
                conn.execute("UPDATE frame_trials SET state=%s,changed_at=%s,changed_by=%s WHERE trial_id=%s",
                             (terminal, now, by, active["trial_id"]))
            return self._open(conn, frame_id, bound, draft, draft_lighting, now, session_id=None,
                              restored_from=None, by=by)

    def _terminal(self, row, bound: BoundFrame | None, now: float) -> str | None:
        """What an active trial row has become at `now`: 'expired' (its lease ran out), 'invalidated'
        (it is not for the Frame's binding now), or None (still active)."""
        if not _lease(row).live(now):
            return "expired"
        if not _current(row, bound):
            return "invalidated"
        return None

    def _open(self, conn, frame_id: str, bound: BoundFrame, draft: Calibration | None,
              draft_lighting: FrameLighting | None, now: float, *, session_id: UUID | None,
              restored_from: UUID | None, by: Actor) -> dict:
        """Insert the Frame's next-generation active trial (the caller holds the locks and has
        ended any active one): at the saved calibration, or at `draft` if its base is it; at the
        saved lighting, or at `draft_lighting`. Its Position history session is `session_id` (a renew's
        predecessor's), or its own id when None (a begin); `restored_from` the entry its draft was
        last restored from."""
        saved = bound.calibration
        if draft is not None and draft.revision != saved.revision:
            raise TrialRefused(TrialRefusal.BASELINE_REVISION_CHANGED)
        calibration = saved if draft is None else draft
        lighting = bound.lighting if draft_lighting is None else draft_lighting
        generation = conn.execute(
            "SELECT COALESCE(max(generation),0)+1 AS n FROM frame_trials WHERE frame_id=%s",
            (frame_id,),
        ).fetchone()["n"]
        trial_id = uuid4()
        session_id = trial_id if session_id is None else session_id
        payload = canonical(calibration.model_dump(mode="json"))
        sha = candidate_hash(trial_id, generation, 1, bound.scope, payload, lighting)
        row = conn.execute(
            "INSERT INTO frame_trials(trial_id,frame_id,generation,player_id,authority_epoch,output_id,"
            "binding_generation,configuration_revision,calibration_revision,sequence,calibration,lighting,"
            "candidate_sha256,state,created_at,touched_at,expires_at,hard_expires_at,session_id,restored_from,"
            "created_by,changed_at,changed_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                trial_id, frame_id, generation, bound.player_id, bound.authority_epoch, bound.scope.output_id,
                bound.scope.binding_generation, bound.scope.config_revision, calibration.revision,
                Jsonb(calibration.model_dump(mode="json")), Jsonb(lighting.model_dump(mode="json")), sha,
                now, now, now + self.inactivity_seconds, now + self.hard_seconds, session_id, restored_from,
                by, now, by,
            ),
        ).fetchone()
        _wake_in(conn, bound.player_id)
        return self._view(row)

    def operate(self, frame_id: str, trial_id: UUID, body: TrialOperation, *, by: Actor) -> dict:
        """One operation on the trial; `edit` and `renew` carry the whole draft (`TrialEdit`)."""
        operation, expected_sequence = body.operation, body.expected_sequence
        proposed = body.calibration if isinstance(body, TrialEdit) else None
        with self.db.transaction() as conn:
            acquire_runtime_locks(conn)
            proposed_lighting = (self.lighting.fitted_within(PgTransaction(conn), body.lighting)
                                 if isinstance(body, TrialEdit) else None)
            terminal = conn.execute(
                "SELECT * FROM frame_trials WHERE trial_id=%s AND frame_id=%s",
                (trial_id, frame_id),
            ).fetchone()
            if terminal is not None and terminal["state"] != "active":
                return self._view(terminal)  # Exact saved/ended retry needs no binding read.
            bound = None
            try:
                bound = bound_frame_in(conn, frame_id)
            except NodeControlError:
                if operation not in ("status", "end", "keepalive"):
                    raise
            row = conn.execute(
                "SELECT * FROM frame_trials WHERE trial_id=%s AND frame_id=%s FOR UPDATE",
                (trial_id, frame_id),
            ).fetchone()
            if row is None:
                raise TrialRefused(TrialRefusal.UNKNOWN)
            now = self.times.now_in(conn)
            if row["state"] == "active":
                ended = self._terminal(row, bound, now)
                if ended:
                    row = conn.execute(
                        "UPDATE frame_trials SET state=%s,changed_at=%s,changed_by=%s WHERE trial_id=%s RETURNING *",
                        (ended, now, by, trial_id),
                    ).fetchone()
                    _wake_in(conn, row["player_id"])
            if operation == "status" or row["state"] != "active":
                return self._view(row)
            if operation == "keepalive":
                # Slides only the idle deadline, never past the hard one; the candidate, its
                # sequence and its presentation are unchanged, so Save stays as it was.
                row = conn.execute(
                    "UPDATE frame_trials SET touched_at=%s,expires_at=%s,changed_at=%s,changed_by=%s "
                    "WHERE trial_id=%s RETURNING *",
                    (now, self._touched(row, now), now, by, trial_id),
                ).fetchone()
                _wake_in(conn, row["player_id"])
                return self._view(row)
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
                    "UPDATE frame_trials SET state='ended',changed_at=%s,changed_by=%s WHERE trial_id=%s",
                    (now, by, trial_id),
                )
                return self._open(conn, frame_id, bound, proposed, proposed_lighting, now,
                                  session_id=row["session_id"], restored_from=row["restored_from"], by=by)
            if operation == "end":
                row = conn.execute(
                    "UPDATE frame_trials SET state='ended',changed_at=%s,changed_by=%s WHERE trial_id=%s "
                    "RETURNING *",
                    (now, by, trial_id),
                ).fetchone()
            elif operation == "edit":
                if proposed.revision != row["calibration_revision"]:
                    raise TrialRefused(TrialRefusal.BASELINE_REVISION_CHANGED)
                row = self._edited_in(conn, row, sequence, proposed, proposed_lighting, now,
                                      restored_from=row["restored_from"], by=by)
            elif operation == "save":
                # The Pi's level-state statement (`TrialReportJudge`): it presents the latest candidate.
                if (row["presented_sequence"] != row["sequence"]
                        or row["presented_sha256"] != row["candidate_sha256"]):
                    raise TrialRefused(TrialRefusal.LATEST_NOT_PRESENTED)
                committed = self.registry.commit_calibration_trial_in(
                    conn,
                    frame_id=frame_id,
                    player_id=row["player_id"],
                    authority_epoch=row["authority_epoch"],
                    output_id=row["output_id"],
                    binding_generation=row["binding_generation"],
                    config_revision=row["configuration_revision"],
                    calibration_revision=row["calibration_revision"],
                    calibration=Calibration.model_validate(row["calibration"]),
                    lighting=FrameLighting.model_validate(row["lighting"]),
                    session_id=row["session_id"],
                    restored_from=row["restored_from"],
                    by=by,
                )
                row = conn.execute(
                    "UPDATE frame_trials SET state='saved',saved_calibration=%s,saved_lighting=%s,"
                    "changed_at=%s,changed_by=%s WHERE trial_id=%s RETURNING *",
                    (Jsonb(committed.model_dump(mode="json")), Jsonb(row["lighting"]), now, by, trial_id),
                ).fetchone()
            _wake_in(conn, row["player_id"])
            return self._view(row)

    def restore(self, frame_id: str, trial_id: UUID, *, entry_id: UUID, expected_sequence: int,
                by: Actor) -> dict:
        """Load a Position history entry into the active trial's draft (configuration snapshots,
        "View and restore"): an `edit` whose calibration and lighting are
        `position_history.restored_draft(entry, base_revision=<the trial's calibration_revision>)`,
        recording `restored_from`; Done still keeps it. Answers the trial's view, as `operate`
        does (the console replaces its draft with the answer's calibration and lighting).
        NodeControlError: trial_sequence_invalid 422 (not an int >= 1), trial_unknown 404,
        trial_ended 410 (the trial is not active, or its lease ran out, or it is not for the Frame's
        binding now), unknown_entry 404 (no such entry, or trimmed), then
        `position_history.restore_refusal` against the Frame's turn and the Display seen on its port
        now (other_frame, made_for_other_turn, made_for_other_display, each 409), then
        trial_sequence_conflict 409, then the entry's lighting against the house as an edit's
        (FrameLightingRefused)."""
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise TrialRefused(TrialRefusal.SEQUENCE_INVALID)
        with self.db.transaction() as conn:
            acquire_runtime_locks(conn)
            bound = None
            with suppress(NodeControlError):
                bound = bound_frame_in(conn, frame_id)
            row = conn.execute(
                "SELECT * FROM frame_trials WHERE trial_id=%s AND frame_id=%s FOR UPDATE",
                (trial_id, frame_id),
            ).fetchone()
            if row is None:
                raise TrialRefused(TrialRefusal.UNKNOWN)
            now = self.times.now_in(conn)
            if row["state"] != "active" or self._terminal(row, bound, now) is not None:
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
            row = self._edited_in(conn, row, sequence, calibration, lighting, now, restored_from=entry_id, by=by)
            _wake_in(conn, row["player_id"])
            return self._view(row)

    def _edited_in(self, conn, row, sequence: int, calibration: Calibration, lighting: FrameLighting,
                   now: float, *,
                   restored_from: UUID | None, by: Actor):
        """The active trial `row` at a new draft (an edit's or a Restore's): its candidate rehashed at
        `sequence`, its presentation cleared, its idle deadline slid, and the entry its draft was last
        restored from. The updated row."""
        sha = candidate_hash(
            row["trial_id"],
            row["generation"],
            sequence,
            _scope(row),
            canonical(calibration.model_dump(mode="json")),
            lighting,
        )
        return conn.execute(
            "UPDATE frame_trials SET sequence=%s,calibration=%s,lighting=%s,candidate_sha256=%s,"
            "touched_at=%s,expires_at=%s,presented_sequence=NULL,presented_sha256=NULL,"
            "presented_at=NULL,restored_from=%s,changed_at=%s,changed_by=%s "
            "WHERE trial_id=%s RETURNING *",
            (
                sequence,
                Jsonb(calibration.model_dump(mode="json")),
                Jsonb(lighting.model_dump(mode="json")),
                sha,
                now,
                self._touched(row, now),
                restored_from,
                now,
                by,
                row["trial_id"],
            ),
        ).fetchone()

    def _touched(self, row, now: float) -> float:
        """The trial's idle deadline after an edit or keepalive at `now` (never past its hard one)."""
        return touched(_lease(row), now, idle_seconds=self.inactivity_seconds).idle_until

    # --- The projection on the display line (the worker's document source) ----------------------

    def trial_in(self, conn: Any, *, frame_id: str, now: float) -> TrialInForce | None:
        """`TrialProjection.trial_in`: the Frame's active trial while its lease is live at `now` and
        it is for the Frame's binding now, with the time left on each lease from `now`; else None.
        Records the sent Position entry (`record_sent_in(..., by="central")`) when what it hands
        differs. Never marks a trial expired or invalidated, and locks no trial row."""
        frame = conn.execute(
            "SELECT f.player_id, f.output_id, f.generation, f.configuration_revision, f.calibration, f.lighting, "
            "p.authority_epoch FROM frames f JOIN players p ON p.id=f.player_id WHERE f.id=%s",
            (frame_id,)).fetchone()
        if frame is None:
            return None
        saved = position_values(Calibration.model_validate(frame["calibration"]),
                                FrameLighting.model_validate(frame["lighting"]))
        row = conn.execute("SELECT * FROM frame_trials WHERE frame_id=%s AND state='active'", (frame_id,)).fetchone()
        if (row is None or not _lease(row).live(now)
                or (row["player_id"], row["authority_epoch"], row["output_id"], row["binding_generation"],
                    row["configuration_revision"]) != (frame["player_id"], frame["authority_epoch"],
                                                       frame["output_id"], frame["generation"],
                                                       frame["configuration_revision"])):
            record_sent_in(conn, frame_id, trial=None, saved=saved, by="central")
            return None
        calibration = Calibration.model_validate(row["calibration"])
        lighting = FrameLighting.model_validate(row["lighting"])
        hard_ms = min(MAX_TRIAL_HARD_MS, max(1, int((row["hard_expires_at"] - now) * 1000)))
        idle_ms = min(hard_ms, max(1, int((row["expires_at"] - now) * 1000)))
        trial = PositionTrial(row["trial_id"], row["generation"], row["sequence"], _scope(row),
                              canonical(row["calibration"]), lighting, row["candidate_sha256"], idle_ms, hard_ms)
        record_sent_in(conn, frame_id, trial=TrialHanded(position_values(calibration, lighting),
                                                         row["session_id"] or row["trial_id"], row["restored_from"]),
                       saved=saved, by="central")
        return TrialInForce(trial, row["expires_at"], row["hard_expires_at"])

    def next_deadline_in(self, conn: Any, *, device_id: str, now: float) -> float | None:
        """`TrialProjection.next_deadline_in`: the earliest idle or hard deadline after `now` of an
        active trial on the Node's Frames."""
        return conn.execute(
            "SELECT min(d.at) AS at FROM frame_trials t JOIN frames f ON f.id=t.frame_id "
            "JOIN players p ON p.id=f.player_id CROSS JOIN LATERAL (VALUES (t.expires_at), (t.hard_expires_at)) d(at) "
            "WHERE p.device_id=%s AND p.retired_at IS NULL AND t.state='active' AND d.at > %s",
            (device_id, now)).fetchone()["at"]


class TrialReportJudge:
    """`RecordJudge` for the display line's state stream: each `trial-…` report sets, on the active
    trial of the Frame bound to its Output, `presented_sequence`, `presented_sha256` and
    `presented_at` (the database's clock, changed_by 'node') when it names that trial's id,
    generation, sequence and candidate, and clears them when it names none (or another). A report
    that does not decode is logged and skipped; a removed key reads as none. Other keys are not
    its. Locks only frame_trials rows."""

    def __init__(self, *, times: TransactionClock = DatabaseTransactionClock()) -> None:
        self._times = times

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None:
        if stream != REPORT_STREAM:
            return
        for item in items:
            report = self._report_of(device_id, item)
            if report is not None:
                self._judge_in(conn, device_id, report)

    @staticmethod
    def _report_of(device_id: str, item: Read) -> TrialReport | None:
        key = item.subject.removeprefix(REPORT_SUBJECT)
        output_id = key.removeprefix("trial-")
        if not key.startswith("trial-") or output_id not in OUTPUT_IDS:
            return None
        if (item.headers or {}).get(KV_OPERATION_HEADER):
            return TrialReport(output_id, None)
        try:
            report = decode_trial_report(item.data)
            if trial_report_key(report.output_id) != key:
                raise ValueError("trial_report_key")
        except ValueError as refused:
            log.warning("trial report refused: device=%s epoch=%s seq=%s key=%s: %s",
                        device_id, item.token.epoch, item.token.seq, key, refused)
            return None
        return report

    def _judge_in(self, conn: Any, device_id: str, report: TrialReport) -> None:
        row = conn.execute(
            "SELECT t.trial_id, t.generation, t.sequence, t.candidate_sha256, t.presented_sequence, "
            "t.presented_sha256 FROM players p JOIN frames f ON f.player_id=p.id "
            "JOIN frame_trials t ON t.frame_id=f.id AND t.state='active' "
            "WHERE p.device_id=%s AND p.retired_at IS NULL AND f.output_id=%s FOR UPDATE OF t",
            (device_id, report.output_id)).fetchone()
        if row is None:
            return
        presented = report.presented
        names = presented is not None and (presented.trial_id, presented.generation, presented.sequence,
                                           presented.candidate_sha256) == (
            row["trial_id"], row["generation"], row["sequence"], row["candidate_sha256"])
        if names and (row["presented_sequence"], row["presented_sha256"]) != (row["sequence"],
                                                                               row["candidate_sha256"]):
            now = self._times.now_in(conn)
            conn.execute("UPDATE frame_trials SET presented_sequence=sequence, presented_sha256=candidate_sha256, "
                         "presented_at=%s, changed_at=%s, changed_by='node' WHERE trial_id=%s",
                         (now, now, row["trial_id"]))
        elif not names and row["presented_sequence"] is not None:
            conn.execute("UPDATE frame_trials SET presented_sequence=NULL, presented_sha256=NULL, presented_at=NULL, "
                         "changed_at=%s, changed_by='node' WHERE trial_id=%s",
                         (self._times.now_in(conn), row["trial_id"]))


def _lease(row) -> Lease:
    """A trial row's lease: its idle deadline (`expires_at`) and its hard one."""
    return Lease(row["expires_at"], row["hard_expires_at"])


log = logging.getLogger(__name__)
