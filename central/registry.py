"""Registry authority: observations cannot mutate desired Frame configuration."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field, StrictBool

from central.actor import Actor
from central.central_errors.ports import BoundOutput
from central.db import Database
from central.fleet.acceptance_evidence import applied_is_latest
from central.fleet.device_fence import fence_in, retire_in
from central.fleet.frame_dark import frame_dark
from central.fleet.models import TrialRefusal, TrialRefused
from central.fleet.position_history import (
    created_record,
    position_values,
    saved_record,
)
from central.frame_lighting.ports import FrameLightingCheck
from central.infra.display_store import (
    POSITION_COLUMNS,
    POSITION_FACTS,
    SCREEN_COLUMNS,
    SCREEN_FACTS,
    SCREEN_JOIN,
    commit_position,
    frame_display_mm,
    frame_readiness,
    frame_screen_px,
    observe_outputs_in,
    observed_outputs_in,
    output_observation_in,
    resolve_output,
)
from central.infra.health_report_store import health_reports_in
from central.infra.output_wakes import notify_nodes_in
from central.infra.player_state_wakes import notify_if_written_in
from central.infra.position_history_store import record_created_in, write_position_in, write_turn_in
from central.infra.power_store import forget_frame_in
from central.infra.transactions import PgTransaction, pg_connection
from central.installation_models import (
    FrameInventory,
    InstallationInventory,
    Name,
    OutputInventory,
    PlayerInventory,
    WallInventory,
    WallNaming,
)
from central.installation_ports import InstallationSessions
from central.installation_repository import PostgresInstallationRepository
from central.kernel.transactions import Transaction
from central.transaction_locks import acquire_runtime_locks, holds_runtime_locks_in
from contracts.display_turn import DisplayTurn
from contracts.enrollment import Enrollment as Enrollment
from contracts.enrollment import OutputReport as OutputReport
from contracts.enrollment import enrollment_message as enrollment_message
from contracts.frame_lighting import FrameLighting
from contracts.models import (
    Calibration,
    ExactTurn,
    Identifier,
    Model,
    OutputBinding,
    TargetIdentifier,
    frame_size_mm,
)
from contracts.node_health import HealthReport
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.player_control import (
    SCHEMA,
    ControlAck,
    ControlAckResponse,
    ControlAppliedReceipt,
    ControlDelivery,
    ControlHello,
    ControlSelection,
    select_control,
)
from contracts.time import Clock


def _frame_inventory(row: Mapping[str, Any], health: Mapping[str, HealthReport]) -> FrameInventory:
    """One Frame's inventory row from a frames row selected with POSITION_COLUMNS and SCREEN_COLUMNS:
    its readiness, its screen, the Display's size and the Frame's size on the wall
    (`frame_size_mm` of its box and that size, as its screen hangs; None when no size is known), and
    whether its bound Output is dark by its Pi's newest health verdict (`health`, by player id)."""
    display_mm = frame_display_mm(row)
    return FrameInventory.model_validate(
        {**{key: value for key, value in row.items() if key not in POSITION_FACTS + SCREEN_FACTS},
         "generation": row["generation"], "readiness": frame_readiness(row), "screen_px": frame_screen_px(row), "display_mm": display_mm,
         "size_mm": frame_size_mm(Calibration.model_validate(row["calibration"]).fit, display_mm, row["turn"]),
         "dark": frame_dark(health.get(row["player_id"]), row["output_id"])})


class RegistryError(Exception):
    def __init__(self, code: str, status: int = 409, *, details: dict | None = None):
        self.code, self.status = code, status
        self.details = details or {}
        super().__init__(code)


class FrameCreate(Model):
    """POST /v1/operator/frames: a new Frame, where it hangs and whether it shows videos (frame box
    run fb-14). Its shape and size are never typed: they come from its Position box on the Display
    it is bound to (owner, 2026-10-19: "diagonal inches is a weird concept when we already have
    width and height")."""
    # A new Frame's id must be reachable by a Scene (contracts TARGET_ID_PATTERN).
    # Stored Frames and path parameters stay `Identifier`, so an older id is still
    # readable and deletable.
    id: TargetIdentifier
    name: Name | None = None              # None: "Frame N", N the smallest free
    surface_id: Identifier = "wall"
    x_mm: float = 0                       # the Frame's top-left on the wall
    y_mm: float = 0
    video: StrictBool = True              # Shows videos (frames.video)


class FramePlacement(Model):
    """PATCH /v1/operator/frames/{frame_id}: where the Frame hangs on the wall (its top-left); a
    field left out keeps its value."""
    surface_id: Identifier | None = None
    x_mm: float | None = None
    y_mm: float | None = None


class FrameVideoChange(Model):
    """PUT /v1/operator/frames/{frame_id}/video: whether the Frame shows videos (its own setting;
    frame box run, bead fb-5). Strict: a JSON "false" or 0 is refused."""
    video: StrictBool


class FrameTurnChange(Model):
    """PUT /v1/operator/frames/{frame_id}/turn: how the Frame's screen hangs (render redesign S2;
    owner gate Q3, 2026-10-17: the turn belongs to the spot on the wall), against the Frame's
    generation the operator read. `turn` is exact (`ExactTurn`): a JSON false, 90.0 or "90" is refused."""
    turn: ExactTurn
    expected_generation: int = Field(ge=0)


def _wake_nodes_of(conn, *player_ids: str | None) -> None:
    """Wake the Output document source of each named Player's Node (None skipped), on commit: every
    write that changes which Frame an Output carries, a bound Frame's turn, or its Position (the
    Frame region document, issue #124) calls it."""
    for player_id in dict.fromkeys(player_id for player_id in player_ids if player_id is not None):
        notify_nodes_in(conn, "SELECT device_id FROM players WHERE id=%s", (player_id,))


def _smallest_free(conn, table: str, word: str) -> str:
    """`word` and the smallest N >= 1 no row of `table` (frames or walls) is named, ignoring case."""
    row = conn.execute(
        f"SELECT min(n) AS n FROM generate_series(1, (SELECT count(*) + 1 FROM {table})) n "  # noqa: S608
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE lower(t.name) = lower(%s || ' ' || n))",
        (word,)).fetchone()
    return f"{word} {row['n']}"


def _ensure_wall(conn, surface_id: str, *, now: float, by: Actor) -> None:
    """A wall row for `surface_id`, named "Wall N" when it is new: every surface a Frame is placed
    on is a named wall."""
    if conn.execute("SELECT 1 FROM walls WHERE surface_id=%s", (surface_id,)).fetchone() is None:
        conn.execute("INSERT INTO walls(surface_id, name, created_at, created_by, changed_at, changed_by) "
                     "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (surface_id) DO NOTHING",
                     (surface_id, _smallest_free(conn, "walls", "Wall"), now, by, now, by))


def _name_or(exc: UniqueViolation, code: str) -> str:
    """name_taken when `exc` is migration 091's case-insensitive Frame name index, else `code`."""
    return "name_taken" if exc.diag.constraint_name == "frames_name" else code


class _EnrolmentNonces:
    """Outstanding enrolment challenges, in this process's memory (decision 0034): at most one per
    public key and `LIMIT` in all, each until its expiry. A challenge is one-time and short-lived,
    so it is never stored: one issued by a Central that restarted since is not held here, and the
    Player's enrolment is refused as expired; the Player asks for a new challenge."""

    LIMIT = 1024

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, tuple[str, float]] = {}   # nonce -> (public key, expires_at)

    def issue(self, nonce: str, public_key: str, expires_at: float, now: float) -> bool:
        """Hold `nonce` for `public_key`, replacing the key's earlier challenge and dropping expired
        ones; False when `LIMIT` challenges are outstanding."""
        with self._lock:
            self._held = {held: (key, expires) for held, (key, expires) in self._held.items()
                          if expires > now and key != public_key}
            if len(self._held) >= self.LIMIT:
                return False
            self._held[nonce] = (public_key, expires_at)
            return True

    def take(self, nonce: str, public_key: str, now: float) -> bool:
        """Use `nonce` once: True when it is held for `public_key` and not expired."""
        with self._lock:
            held = self._held.get(nonce)
            if held is None or held[0] != public_key or held[1] <= now:
                return False
            del self._held[nonce]
            return True


def _control_document(epoch: int, status: str) -> dict:
    """A new control session (`players.control_session`) for enrolment `epoch`: nothing offered,
    issued or acknowledged yet."""
    return {"authority_epoch": epoch, "status": status, "capabilities": [], "offered_schemas": None,
            "offered_capabilities": None, "issued_sequence": 0, "pending_id": None, "pending_digest": None,
            "pending_expires": None, "applied_sequence": 0, "applied_at": None, "applied_delivery_id": None,
            "applied_digest": None, "applied_ack_nonce": None, "last_delivery_id": None,
            "last_result_sequence": None, "last_result_digest": None, "last_result": None,
            "last_result_at": None}


# The control session's facts `control_fact` reads (an operator's provenance read).
_CONTROL_FACTS = ("authority_epoch", "status", "issued_sequence", "pending_id", "pending_digest",
                  "applied_sequence", "applied_at", "applied_delivery_id", "applied_digest",
                  "last_delivery_id", "last_result_sequence", "last_result_digest", "last_result",
                  "last_result_at")


class Registry:
    IDENTIFY_OUTPUT_TTL_SECONDS = 15

    def __init__(self, db: Database, clock: Clock, *, lighting: FrameLightingCheck,
                 installation: InstallationSessions | None = None):
        """`lighting`: the Frame-lighting check every lighting write goes through (the composition
        root's PgFrameLighting). `installation`: the Installation port that holds the one query
        of a Player's configuration (PostgreSQL's unless given, as Coordinator's)."""
        self.db, self.clock = db, clock
        self.lighting = lighting
        self.installation: InstallationSessions = installation or PostgresInstallationRepository(clock)
        self._nonces = _EnrolmentNonces()

    @contextmanager
    def _equipment_write(self):
        """Serialize equipment mutation after Coordination then Runtime locks."""
        with self.db.transaction() as conn:
            acquire_runtime_locks(conn)
            yield conn
            # A Player's sockets wake when an equipment change commits (player_state_wakes.py).
            notify_if_written_in(conn)

    def challenge(self, public_key: str) -> dict:
        # Endpoint schema also validates; keep this boundary safe for non-HTTP callers.
        try:
            raw = bytes.fromhex(public_key)
            if len(raw) != 32 or raw.hex() != public_key:
                raise ValueError
            Ed25519PublicKey.from_public_bytes(raw)
        except ValueError as exc:
            raise RegistryError("invalid_key", 422) from exc
        now, nonce = self.clock.utc(), secrets.token_hex(32)
        # A bounded outstanding challenge per key avoids unbounded growth on ordinary retry.
        if not self._nonces.issue(nonce, public_key, now + 60, now):
            raise RegistryError("enrollment_busy", 429)
        return {"nonce": nonce, "expires_at": now + 60}

    def enroll(self, request: Enrollment) -> dict:
        try:
            signature = base64.b64decode(request.signature, validate=True)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(request.public_key)).verify(
                signature, enrollment_message(request.nonce, request.outputs, request.device_id,
                                              request.boot_id)
            )
        except (ValueError, InvalidSignature) as exc:
            raise RegistryError("invalid_proof", 403) from exc
        now = self.clock.utc()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        player_id = "p-" + hashlib.sha256(request.device_id.encode()).hexdigest()[:32]
        with self.db.transaction() as conn:
            from central.coordination import COORDINATION_LOCK

            conn.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            if not self._nonces.take(request.nonce, request.public_key, now):
                raise RegistryError("expired_or_used_challenge", 403)
            # Serial enrollment has no OS command authority. It must still respect permanent
            # device retirement: it acts under the device fence that retirement changes under.
            # A device never offered (a Compose or demo Player) is unrecorded and admitted.
            standing = fence_in(conn, request.device_id, "act").standing()
            if standing.kind == "unavailable":
                raise RegistryError("retired_device", 403)
            old = conn.execute("SELECT * FROM players WHERE device_id=%s FOR UPDATE",
                               (request.device_id,)).fetchone()
            if old and old["retired_at"] is not None:
                raise RegistryError("retired_player", 403)
            if old:
                conn.execute("UPDATE players SET public_key=%s,token_hash=%s,last_seen=%s,"
                             "authority_epoch=authority_epoch+1,changed_at=%s,changed_by='node' WHERE id=%s",
                             (request.public_key, token_hash, now, now, player_id))
            else:
                conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,device_id,"
                             "created_at,created_by,changed_at,changed_by) "
                             "VALUES(%s,%s,%s,%s,%s,%s,%s,'node',%s,'node')",
                             (player_id, request.public_key, token_hash, now, now,
                              request.device_id, now, now))
            observe_outputs_in(conn, player_id, {output.output_id: output.model_dump()
                                                 for output in request.outputs}, now)
            epoch = conn.execute("SELECT authority_epoch FROM players WHERE id=%s",
                                 (player_id,)).fetchone()["authority_epoch"]
            # Negotiation belongs to this enrollment epoch, never to the serial or package tag.
            self._reset_control(conn, player_id, epoch, "open", now)
            # Every Player enrolls unbound (pending) by serial alone; its boot is recorded.
            conn.execute("UPDATE players SET health=health || %s WHERE id=%s",
                         (Jsonb({"boot_id": request.boot_id}), player_id))
            # The previous enrollment's socket wakes, to find its authority gone.
            notify_if_written_in(conn)
        return {"player_id": player_id, "token": token, "authority_epoch": epoch}

    def authenticate(self, token: str) -> dict:
        """The Player `token` names; it writes nothing (a Player's liveness is its session
        socket's open and close: central/infra/player_contact_store.py)."""
        if not 32 <= len(token) <= 256:
            raise RegistryError("unauthorized", 401)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id,authority_epoch FROM players WHERE token_hash=%s "
                               "AND retired_at IS NULL", (digest,)).fetchone()
            if not row:
                raise RegistryError("unauthorized", 401)
            return row

    @staticmethod
    def _control_player(conn, player_id: str, epoch: int, *, lock: bool = True) -> None:
        """Refuse unless `epoch` is `player_id`'s current, unretired enrollment. `lock` row-locks
        the Player against a concurrent enrollment, for a caller about to write its control
        session; a read (`lock=False`) takes no row lock and writes nothing, so a poll of an
        unchanged state or a re-sent acknowledgment never waits on, or for, node admission's
        `FOR SHARE` (2026-11-16: a write per poll)."""
        row = conn.execute(
            "SELECT authority_epoch,retired_at FROM players WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (player_id,),
        ).fetchone()
        if not row or row["retired_at"] is not None or row["authority_epoch"] != epoch:
            raise RegistryError("stale_authority", 403)

    @staticmethod
    def _control_session_in(conn, player_id: str) -> dict | None:
        """The Player's control session (`players.control_session`); None before its first
        enrolment opened one."""
        row = conn.execute("SELECT control_session FROM players WHERE id=%s", (player_id,)).fetchone()
        return None if row is None else row["control_session"]

    @staticmethod
    def _write_control(conn, player_id: str, changes: Mapping[str, Any], now: float) -> None:
        """Merge `changes` into the Player's control session, under the Player's row lock (the
        Player's own request: `node`)."""
        conn.execute("UPDATE players SET control_session=control_session || %s, changed_at=%s, "
                     "changed_by='node' WHERE id=%s", (Jsonb(dict(changes)), now, player_id))

    @staticmethod
    def _reset_control(conn, player_id: str, epoch: int, status: str, now: float) -> None:
        conn.execute("UPDATE players SET control_session=%s, changed_at=%s, changed_by='node' WHERE id=%s",
                     (Jsonb(_control_document(epoch, status)), now, player_id))

    def control_hello(self, player_id: str, request: ControlHello) -> ControlSelection:
        with self.db.transaction() as conn:
            self._control_player(conn, player_id, request.authority_epoch)
            now = self.clock.utc()
            row = self._control_session_in(conn, player_id)
            current = row is not None and row["authority_epoch"] == request.authority_epoch
            if current and row["status"] == "negotiated":
                if (row["offered_schemas"] != list(request.schemas)
                        or row["offered_capabilities"] != list(request.capabilities)):
                    raise RegistryError("control_negotiation_conflict")
                return ControlSelection(authority_epoch=request.authority_epoch,
                                        schema=SCHEMA,
                                        capabilities=tuple(row["capabilities"]))
            selection = select_control(request)
            if selection is None:
                raise RegistryError("control_schema_unsupported")
            if not current:
                # Enrollment opens each epoch's row; the epoch is current (checked above).
                self._reset_control(conn, player_id, request.authority_epoch, "open", now)
            self._write_control(conn, player_id, {
                "status": "negotiated", "capabilities": list(selection.capabilities),
                "offered_schemas": list(request.schemas),
                "offered_capabilities": list(request.capabilities)}, now)
            return selection

    def control_selection(self, player_id: str, epoch: int) -> ControlSelection:
        with self.db.transaction() as conn:
            return self.control_selection_in(conn, player_id, epoch)

    def control_selection_in(self, conn, player_id: str, epoch: int) -> ControlSelection:
        """The epoch's negotiated selection, read under a caller-owned Coordination
        transaction when issuing state. State is served only after the hello. A read: no row
        lock."""
        self._control_player(conn, player_id, epoch, lock=False)
        row = self._control_session_in(conn, player_id)
        if not row or row["authority_epoch"] != epoch or row["status"] != "negotiated":
            raise RegistryError("control_negotiation_required")
        return ControlSelection(authority_epoch=epoch, schema=SCHEMA,
                                capabilities=tuple(row["capabilities"]))

    def issue_control_delivery_record(self, player_id: str, epoch: int,
                                      digest: str) -> dict:
        """Persist a challenge and sequence before sending either state transport."""
        with self.db.transaction() as conn:
            return self.issue_control_delivery_record_in(conn, player_id, epoch, digest)

    def issue_control_delivery_record_in(self, conn, player_id: str, epoch: int,
                                         digest: str) -> dict:
        """Issue under the caller's Coordinator lock, atomic with its projected state. The same
        state again is the same delivery, read without a row lock or a write; only a new one
        row-locks the Player and is written."""
        for lock in (False, True):
            self._control_player(conn, player_id, epoch, lock=lock)
            now = self.clock.utc()      # sampled after any wait for the row lock
            row = self._control_session_in(conn, player_id)
            if not row or row["authority_epoch"] != epoch or row["status"] != "negotiated":
                raise RegistryError("control_protocol_mismatch")
            if (row["pending_id"] and row["pending_digest"] == digest
                    and row["pending_expires"] > now):
                return ControlDelivery(delivery_id=row["pending_id"],
                                       delivery_sequence=row["issued_sequence"]).model_dump()
            if applied_is_latest(row) and row["applied_digest"] == digest:
                # Unchanged since the Player applied it: the same delivery again, so its receipt
                # (and an app link proved with it) stays current until the state really changes.
                return ControlDelivery(delivery_id=row["applied_delivery_id"],
                                       delivery_sequence=row["applied_sequence"]).model_dump()
        delivery_id = secrets.token_hex(16)
        issued = row["issued_sequence"] + 1
        self._write_control(conn, player_id, {
            "issued_sequence": issued, "pending_id": delivery_id, "pending_digest": digest,
            "pending_expires": now + 60, "applied_ack_nonce": None}, now)
        return ControlDelivery(delivery_id=delivery_id, delivery_sequence=issued).model_dump()

    @staticmethod
    def _current_applied_receipt(row: dict, report: ControlAck) -> ControlAppliedReceipt | None:
        """Replay only a receipt for the exact still-current applied delivery."""
        if (report.result != "applied" or not applied_is_latest(row)
                or row["applied_delivery_id"] != report.delivery_id):
            return None
        return ControlAppliedReceipt(
            authority_epoch=row["authority_epoch"],
            delivery_id=row["applied_delivery_id"],
            delivery_sequence=row["applied_sequence"],
            state_digest=row["applied_digest"],
            ack_nonce=row["applied_ack_nonce"],
        )

    def control_ack_response(self, player_id: str, report: ControlAck) -> ControlAckResponse:
        """Record the Player's acknowledgment of a delivery. One already handled (the Player sent
        it again) is answered as before, its receipt replayed, read without a row lock or a write
        (2026-11-16: a `FOR UPDATE` per state poll); only one that records a result row-locks the
        Player and writes."""
        with self.db.transaction() as conn:
            replay = self._handled_ack_in(conn, player_id, report)
            if replay is not None:
                return replay
            self._control_player(conn, player_id, report.authority_epoch)
            now = self.clock.utc()
            row = self._control_session_in(conn, player_id)
            if (not row or row["authority_epoch"] != report.authority_epoch
                    or row["status"] != "negotiated"):
                return ControlAckResponse(accepted=False)
            if row["pending_id"] != report.delivery_id:
                if (report.result != "applied" and applied_is_latest(row)
                        and row["applied_delivery_id"] == report.delivery_id):
                    # The Player re-applied the re-sent delivery and it did not take: that is
                    # the latest result, so the receipt is no longer current and the next
                    # poll issues a fresh delivery. A partial application never counts.
                    self._write_control(conn, player_id, {
                        "last_result": report.result, "last_result_at": now, "applied_ack_nonce": None}, now)
                    return ControlAckResponse(accepted=True)
                receipt = self._current_applied_receipt(row, report)
                return ControlAckResponse(accepted=receipt is not None, receipt=receipt)
            if row["pending_expires"] is None or row["pending_expires"] <= now:
                return ControlAckResponse(accepted=False)
            # The nonce is generated only after the ACK passes the current-epoch,
            # pending-delivery and expiry checks. It is committed with the result.
            nonce = secrets.token_hex(32) if report.result == "applied" else None
            changes = {"pending_id": None, "pending_digest": None, "pending_expires": None,
                       "last_delivery_id": report.delivery_id, "last_result_sequence": row["issued_sequence"],
                       "last_result_digest": row["pending_digest"], "last_result": report.result,
                       "last_result_at": now, "applied_ack_nonce": nonce}
            if report.result == "applied":
                changes |= {"applied_sequence": row["issued_sequence"], "applied_at": now,
                            "applied_delivery_id": row["pending_id"], "applied_digest": row["pending_digest"]}
            self._write_control(conn, player_id, changes, now)
            receipt = None
            if nonce is not None:
                receipt = ControlAppliedReceipt(
                    authority_epoch=report.authority_epoch,
                    delivery_id=report.delivery_id,
                    delivery_sequence=row["issued_sequence"],
                    state_digest=row["pending_digest"],
                    ack_nonce=nonce,
                )
            return ControlAckResponse(accepted=True, receipt=receipt)

    def _handled_ack_in(self, conn, player_id: str,
                        report: ControlAck) -> ControlAckResponse | None:
        """The answer to an acknowledgment that records nothing, read without a lock: refused
        (no negotiated session on its epoch) or a delivery no longer pending, whose receipt is
        replayed; None when it records a result (the pending delivery, or a re-applied one that
        did not take), which `control_ack_response` writes under the row lock."""
        self._control_player(conn, player_id, report.authority_epoch, lock=False)
        row = self._control_session_in(conn, player_id)
        if (not row or row["authority_epoch"] != report.authority_epoch
                or row["status"] != "negotiated"):
            return ControlAckResponse(accepted=False)
        if row["pending_id"] == report.delivery_id or (
                report.result != "applied" and applied_is_latest(row)
                and row["applied_delivery_id"] == report.delivery_id):
            return None
        receipt = self._current_applied_receipt(row, report)
        return ControlAckResponse(accepted=receipt is not None, receipt=receipt)

    def control_fact(self, player_id: str) -> dict | None:
        """Read provenance-labelled app-control history without readiness inference."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT authority_epoch, control_session FROM players WHERE id=%s "
                               "AND retired_at IS NULL", (player_id,)).fetchone()
        session = None if row is None else row["control_session"]
        if session is None or session["authority_epoch"] != row["authority_epoch"]:
            return None
        return {fact: session[fact] for fact in _CONTROL_FACTS}

    def identify_output(self, player_id: str, output_id: str, *, by: Actor) -> dict:
        """Ask one active, connected, unbound Output to identify itself briefly: one request per
        Player, kept in its show state (`player_show_state.identify`, its epoch inside it), which
        Coordination's delivery serves and clears once spent."""
        now = self.clock.utc()
        request_id = uuid.uuid4()
        expires_at = now + self.IDENTIFY_OUTPUT_TTL_SECONDS
        with self._equipment_write() as conn:
            player = conn.execute(
                "SELECT authority_epoch, control_session FROM players WHERE id=%s AND retired_at IS NULL "
                "FOR UPDATE", (player_id,),
            ).fetchone()
            if not player:
                raise RegistryError("unknown_or_retired_player", 404)
            session = player["control_session"]
            if (session is None or session["authority_epoch"] != player["authority_epoch"]
                    or session["status"] != "negotiated" or "identify_output" not in session["capabilities"]):
                raise RegistryError("identify_unsupported")
            observation = output_observation_in(conn, player_id, output_id, lock=True)
            if observation is None:
                raise RegistryError("unknown_output", 404)
            if not observation.get("connected", False):
                raise RegistryError("output_disconnected")
            if conn.execute("SELECT 1 FROM frames WHERE player_id=%s AND output_id=%s",
                            (player_id, output_id)).fetchone():
                raise RegistryError("output_bound")
            request = {"request_id": str(request_id), "output_id": output_id,
                       "authority_epoch": player["authority_epoch"], "created_at": now,
                       "expires_at": expires_at}
            conn.execute(
                "INSERT INTO player_show_state(player_id,authority_epoch,identify,created_at,"
                "created_by,changed_at,changed_by) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(player_id) DO UPDATE SET identify=EXCLUDED.identify,"
                "changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by",
                (player_id, player["authority_epoch"], Jsonb(request), now, by, now, by),
            )
        return {"request_id": str(request_id), "output_id": output_id,
                "expires_at": expires_at}

    def create_frame(self, frame: FrameCreate, *, by: Actor) -> dict:
        """A new Frame at the default Position (`Calibration()`: the whole screen), showing videos as
        `frame.video` says, with the lighting a new Frame starts with (a new Frame is bound to no
        port yet, so no Display is seen: UPGRADE_DISPLAY_LIGHT), and its KEPT_CREATED Position
        entry, in one transaction."""
        try:
            now = self.clock.utc()
            with self.db.transaction() as conn:
                name = frame.name if frame.name is not None else _smallest_free(conn, "frames", "Frame")
                calibration = Calibration()
                lighting = self.lighting.new_frame_within(PgTransaction(conn), UPGRADE_DISPLAY_LIGHT)
                conn.execute("INSERT INTO frames(id,name,surface_id,x_mm,y_mm,video,calibration,lighting,"
                             "created_at,created_by,changed_at,changed_by) "
                             "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                             (frame.id, name, frame.surface_id, frame.x_mm, frame.y_mm, frame.video,
                              Jsonb(calibration.model_dump(mode="json")), Jsonb(lighting.model_dump(mode="json")),
                              now, by, now, by))
                record_created_in(conn, frame.id, created_record(position_values(calibration, lighting)), by=by)
                _ensure_wall(conn, frame.surface_id, now=now, by=by)
        except UniqueViolation as exc:
            raise RegistryError(_name_or(exc, "frame_exists")) from exc
        return {**frame.model_dump(), "name": name}

    def rename_frame(self, frame_id: str, name: str, *, by: Actor) -> dict:
        """Name a Frame (#94). RegistryError unknown_frame 404, name_taken 409 (ignoring case)."""
        try:
            with self.db.transaction() as conn:
                row = conn.execute("UPDATE frames SET name=%s, changed_at=%s, changed_by=%s WHERE id=%s RETURNING id",
                                   (name, self.clock.utc(), by, frame_id)).fetchone()
                if row is None:
                    raise RegistryError("unknown_frame", 404)
        except UniqueViolation as exc:
            raise RegistryError(_name_or(exc, "frame_exists")) from exc
        return {"frame_id": frame_id, "name": name}

    def set_frame_video(self, frame_id: str, video: bool, *, by: Actor) -> dict:
        """Whether the Frame shows videos (frames.video): {"frame_id", "video", "changed"}.
        RegistryError unknown_frame 404. Last write wins: no generation, configuration revision or
        Position check (it changes planning, not the picture), so a ready Frame stays ready. The Planner reads it at its next plan, as
        it reads a saved photo-fit setting (nothing to wake)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT video FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
            if row is None:
                raise RegistryError("unknown_frame", 404)
            if row["video"] == video:
                return {"frame_id": frame_id, "video": video, "changed": False}
            conn.execute("UPDATE frames SET video=%s, changed_at=%s, changed_by=%s WHERE id=%s",
                         (video, self.clock.utc(), by, frame_id))
        return {"frame_id": frame_id, "video": video, "changed": True}

    def name_wall(self, surface_id: str, naming: WallNaming, *, by: Actor) -> WallInventory:
        """Name a wall and label its room (#92). RegistryError unknown_wall 404."""
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE walls SET name=%s, room=%s, changed_at=%s, changed_by=%s WHERE surface_id=%s "
                               "RETURNING surface_id, name, room",
                               (naming.name, naming.room, self.clock.utc(), by, surface_id)).fetchone()
            if row is None:
                raise RegistryError("unknown_wall", 404)
        return WallInventory.model_validate(row)

    def bind(self, frame_id: str, player_id: str, output_id: str, *, expected_generation: int,
             by: Actor) -> dict:
        """Bind the Frame to the Player's Output (`frames.player_id`, `output_id`; one Frame per
        Output: output_already_bound 409)."""
        try:
            with self._equipment_write() as conn:
                # Common lock ordering for bind and retire avoids transferring retired equipment.
                player = conn.execute("SELECT retired_at FROM players WHERE id=%s FOR UPDATE",
                                      (player_id,)).fetchone()
                if not player or player["retired_at"] is not None:
                    raise RegistryError("unknown_or_retired_player", 404)
                frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE",
                                     (frame_id,)).fetchone()
                if not frame:
                    raise RegistryError("unknown_frame", 404)
                if output_observation_in(conn, player_id, output_id) is None:
                    raise RegistryError("unknown_output", 404)
                if (frame["player_id"], frame["output_id"]) == (player_id, output_id):
                    return {"generation": frame["generation"], "changed": False}
                if frame["generation"] != expected_generation:
                    raise RegistryError("binding_generation_conflict")
                row = conn.execute("UPDATE frames SET player_id=%s,output_id=%s,generation=generation+1,"
                                   "configuration_revision=configuration_revision+1,changed_at=%s,changed_by=%s "
                                   "WHERE id=%s RETURNING generation",
                                   (player_id, output_id, self.clock.utc(), by, frame_id)).fetchone()
                # The Output now carries this Frame (its timetable and turn), and the one it left
                # carries none: wake both Nodes' document sources, on commit.
                _wake_nodes_of(conn, player_id, frame["player_id"])
                resolve_output(conn, player_id, output_id)   # the display there, under this binding
                return {**row, "changed": True}
        except UniqueViolation as exc:
            raise RegistryError("output_already_bound") from exc

    def unbind(self, frame_id: str, *, expected_generation: int, by: Actor) -> dict:
        """Release a Frame's active binding, reversibly: unlike retire, the player record
        (and its ability to be re-bound, to this or another Frame) is untouched."""
        with self._equipment_write() as conn:
            frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            if frame["player_id"] is None:
                raise RegistryError("not_bound", 404)
            if frame["generation"] != expected_generation:
                raise RegistryError("binding_generation_conflict")
            _wake_nodes_of(conn, frame["player_id"])   # its Output now carries no Frame (turn 0)
            row = conn.execute("UPDATE frames SET player_id=NULL,output_id=NULL,generation=generation+1,"
                               "configuration_revision=configuration_revision+1,changed_at=%s,changed_by=%s "
                               "WHERE id=%s RETURNING generation",
                               (self.clock.utc(), by, frame_id)).fetchone()
            return {**row, "changed": True}

    def place_frame(self, frame_id: str, placement: FramePlacement, *, by: Actor) -> dict:
        """Move the Frame on the wall (its top-left): last-write-wins, no token (the one deliberate
        exception to R3, design §9a). FOR UPDATE makes the read-merge-write atomic; the row is
        still LWW. Bumps NO generation, NO configuration_revision; never touches calibration.
        Answers {"id", "surface_id", "x_mm", "y_mm"}."""
        with self._equipment_write() as conn:
            frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE",
                                 (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            merged = {field: value if value is not None else frame[field]
                      for field, value in placement.model_dump().items()}
            now = self.clock.utc()
            conn.execute("UPDATE frames SET surface_id=%s,x_mm=%s,y_mm=%s,changed_at=%s,changed_by=%s WHERE id=%s",
                         (merged["surface_id"], merged["x_mm"], merged["y_mm"], now, by, frame_id))
            _ensure_wall(conn, merged["surface_id"], now=now, by=by)
            return {"id": frame_id, **merged}

    def turn_frame(self, frame_id: str, turn: DisplayTurn, *, expected_generation: int, by: Actor) -> dict:
        """Set how the Frame's screen hangs (frames.turn), in one equipment write: RegistryError
        unknown_frame 404, then binding_generation_conflict 409 when `expected_generation` is not the
        Frame's (the token before idempotency); the same turn answers
        {"frame_id", "turn", "generation", "changed": False} with no write. A new turn raises the
        generation and configuration_revision (a Position made for the other hanging needs checking:
        readiness POSITION_NEEDED until the next Save) and appends the kept-turn-changed Position history
        entry (`write_turn_in`: the Position as it is, made for the new turn, so Restore refuses the
        entries made for the old one) and wakes the bound Player's Node's document source (`notify_nodes_in`), so its Output
        document carries the turn without waiting for a deadline; answers {..., "changed": True}.
        Never touches the Position, the binding or the lighting."""
        with self._equipment_write() as conn:
            frame = conn.execute("SELECT generation, turn, player_id FROM frames WHERE id=%s FOR UPDATE",
                                 (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            if frame["generation"] != expected_generation:
                raise RegistryError("binding_generation_conflict")
            if frame["turn"] == turn:
                return {"frame_id": frame_id, "turn": turn, "generation": frame["generation"],
                        "changed": False}
            row = {"generation": write_turn_in(conn, frame_id, turn, now=self.clock.utc(), by=by)}
            _wake_nodes_of(conn, frame["player_id"])
            return {"frame_id": frame_id, "turn": turn, **row, "changed": True}

    def delete_frame(self, frame_id: str, *, conn, references: Mapping[str, tuple[str, ...]],
                     by: Actor) -> dict:
        """Remove a clear Frame within the caller's serialized reference snapshot.

        Callers must obtain `references` from the current Runtime while holding the
        Coordination and Runtime locks on this same transaction connection. Requiring
        both arguments prevents this Registry boundary from silently treating an
        unavailable snapshot as an empty set. The row lock and binding guard remain
        atomic here; the complete snapshot supplies the Scene/queue/live-Run guard.
        """
        reference_keys = {"scene_ids", "program_ids", "queued_activation_ids", "run_ids"}
        if conn is None or not isinstance(references, Mapping) or set(references) != reference_keys:
            raise RegistryError("frame_reference_snapshot_required", 500)
        if any(not isinstance(references[key], tuple) for key in reference_keys):
            raise RegistryError("frame_reference_snapshot_required", 500)
        # The caller's reference snapshot is authoritative only while both
        # writers are excluded on this same transaction connection.
        if not holds_runtime_locks_in(conn):
            raise RegistryError("frame_reference_snapshot_required", 500)
        frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
        if not frame:
            raise RegistryError("unknown_frame", 404)
        if frame["player_id"] is not None:
            raise RegistryError("frame_bound")
        if references.get("scene_ids") or references.get("queued_activation_ids"):
            raise RegistryError("frame_referenced", 409, details={
                "scene_ids": list(references.get("scene_ids", ())),
                "program_ids": list(references.get("program_ids", ())),
                "queued_activation_ids": list(references.get("queued_activation_ids", ())),
            })
        conn.execute("DELETE FROM frames WHERE id=%s", (frame_id,))
        forget_frame_in(conn, frame_id, by=by)
        return {"status": "deleted"}

    def retire(self, player_id: str, *, by: Actor) -> None:
        """Retire a Player permanently. Refuses (409 player_bound), for every caller,
        while any of its Outputs is bound: replacement is unbind, then retire.

        The binding check runs AFTER the Player row lock, which bind takes first
        too, so a bind and a retire serialize. Under READ COMMITTED (the default;
        central/db.py sets no isolation) the check is a new statement after any lock
        wait and sees a bind that committed during it. Retiring twice is a no-op."""
        with self._equipment_write() as conn:
            identity = conn.execute("SELECT device_id FROM players WHERE id=%s",
                                    (player_id,)).fetchone()
            if identity is None:
                raise RegistryError("unknown_player", 404)
            device_id = identity["device_id"]
            fenced = fence_in(conn, device_id, "change")
            now = self.clock.utc()
            player = conn.execute("SELECT * FROM players WHERE id=%s FOR UPDATE", (player_id,)).fetchone()
            if player["retired_at"] is not None:
                return
            if conn.execute("SELECT 1 FROM frames WHERE player_id=%s LIMIT 1",
                            (player_id,)).fetchone():
                raise RegistryError("player_bound")
            # A Player that never booted from a node offer (a Compose or demo Player) has no
            # `devices` row; retirement writes its canonical tombstone.
            retire_in(fenced, now, by=by)
            conn.execute("UPDATE players SET retired_at=%s,authority_epoch=authority_epoch+1,"
                         "changed_at=%s,changed_by=%s WHERE id=%s", (now, now, by, player_id))

    @staticmethod
    def calibration_mode_in(conn, frame_id: str) -> str:
        """`native_trial` when the Frame's bound Player runs on a Node Central admitted (its
        device holds a node boot admission, current or past); otherwise `not_admitted`: a Player
        that never booted from a node offer (a Compose or demo Player) has no calibration
        protocol. A revoked admission does not withdraw the capability."""
        row = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM frames f JOIN players p ON p.id=f.player_id "
            "JOIN node_boot_admissions a ON a.device_id=p.device_id "
            "WHERE f.id=%s) AS native",
            (frame_id,),
        ).fetchone()
        return "native_trial" if row["native"] else "not_admitted"

    @staticmethod
    def bound_output_within(tx: Transaction, frame_id: str) -> BoundOutput | None:
        """central_errors.ports FrameOutputs: the Frame's bound, not retired Player's device and
        Output; None when unbound or the Player has no device."""
        row = pg_connection(tx).execute(
            "SELECT f.output_id, p.device_id FROM frames f JOIN players p ON p.id = f.player_id "
            "AND p.retired_at IS NULL WHERE f.id=%s", (frame_id,)).fetchone()
        if row is None or row["device_id"] is None:
            return None
        return BoundOutput(device_id=row["device_id"], output_id=row["output_id"])

    def calibration_capability(self, frame_id: str) -> dict:
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM frames WHERE id=%s", (frame_id,)).fetchone():
                raise RegistryError("unknown_frame", 404)
            return {"mode": self.calibration_mode_in(conn, frame_id)}

    def commit_calibration_trial_in(self, conn, *, frame_id: str, player_id: str,
            authority_epoch: int, output_id: str, binding_generation: int,
            config_revision: int, calibration_revision: int,
            calibration: Calibration, lighting: FrameLighting, session_id: uuid.UUID | None,
            restored_from: uuid.UUID | None, by: Actor) -> Calibration:
        """Registry-owned Save CAS inside the Trial owner's existing transaction.

        Caller holds Coordination→Runtime, then the device fence and session locks. This method
        owns the persistent Frame calibration and lighting (the lighting rides the binding beside the
        calibration and is saved with it; it never changes the Frame output). It never interprets
        trial evidence. It is a kept Position write: a change made outside Photo Wall is recorded
        first, then the Save's kept-saved entry
        (the trial's `session_id`, and `restored_from` when its draft came from a Restore), in this
        transaction. A box always lies on its screen (contracts/frame_fit.py `FrameBox`), so Save
        reads no screen size and checks nothing about where the box lies. It wakes the bound Player's
        Node's document source on commit, so its Frame region document (issue #124) carries the saved
        box without waiting for a deadline.
        """
        if not holds_runtime_locks_in(conn):
            raise RegistryError("calibration_runtime_locks_required")
        row = conn.execute("SELECT f.*,p.authority_epoch,p.retired_at "
                           "FROM frames f JOIN players p ON p.id=f.player_id "
                           "WHERE f.id=%s FOR UPDATE OF f",
                           (frame_id,)).fetchone()
        if row is None or row["retired_at"] is not None or (
            row["player_id"], row["authority_epoch"], row["output_id"], row["generation"],
            row["configuration_revision"]
        ) != (player_id, authority_epoch, output_id, binding_generation, config_revision):
            raise TrialRefused(TrialRefusal.CALIBRATION_BASELINE_CHANGED)
        current = Calibration.model_validate(row["calibration"])
        if current.revision != calibration_revision:
            raise TrialRefused(TrialRefusal.CALIBRATION_REVISION_CONFLICT)
        committed = Calibration.model_validate(calibration.model_dump()).model_copy(
            update={"revision": current.revision + 1})
        now = self.clock.utc()
        write_position_in(conn, frame_id, saved_record(position_values(committed, lighting), session_id=session_id,
                                                       restored_from=restored_from), calibration=committed,
                          now=now, by=by)
        commit_position(conn, frame_id, now=now, by=by)
        _wake_nodes_of(conn, player_id)
        return committed

    def bindings_for(self, player_id: str, epoch: int, *, include_unvalidated=False) -> list[OutputBinding]:
        config = self.configuration_for(player_id, epoch)
        return config["bindings" if include_unvalidated else "execution_bindings"]

    def configuration_for(self, player_id: str, epoch: int) -> dict[str, list[OutputBinding]]:
        with self.db.transaction() as conn:
            return self.configuration_in(conn, player_id, epoch)

    def configuration_in(self, conn, player_id: str, epoch: int) -> dict[str, list[OutputBinding]]:
        """Hold registry authority through the caller's offer/configuration transaction: the one query
        and mapping, the Installation port's `configuration_in`; stale_authority 403 when the
        session is not current."""
        configuration = self.installation.configuration_in(conn, player_id, epoch)
        if configuration is None:
            raise RegistryError("stale_authority", 403)
        return configuration

    def inventory(self) -> InstallationInventory:
        with self.db.transaction() as conn:
            return self.inventory_in(conn, self.clock.utc())

    def inventory_in(self, conn, now: float) -> InstallationInventory:
        """Read inventory in a caller-owned transaction; safe in read-only snapshots."""
        players = conn.execute("SELECT id,device_id,authority_epoch,registered_at,last_seen,retired_at,health "
                               "FROM players ORDER BY registered_at,id").fetchall()
        outputs = observed_outputs_in(conn)
        frames = conn.execute("SELECT f.id,f.name,f.surface_id,f.x_mm,f.y_mm,f.video,"  # noqa: S608
                              "f.calibration,f.lighting,f.turn,f.configuration_revision,"
                              f"f.player_id,f.output_id,{POSITION_COLUMNS},{SCREEN_COLUMNS} FROM frames f "
                              f"{SCREEN_JOIN} ORDER BY f.id").fetchall()
        bound_player_ids = {frame["player_id"] for frame in frames if frame["player_id"] is not None}
        health = health_reports_in(conn)
        return InstallationInventory(
            players=tuple(PlayerInventory.model_validate({**row, "is_bound": row["id"] in bound_player_ids})
                         for row in players),
            outputs=tuple(OutputInventory.model_validate(row) for row in outputs),
            frames=tuple(_frame_inventory(row, health) for row in frames),
            walls=tuple(WallInventory.model_validate(row) for row in conn.execute(
                "SELECT surface_id, name, room FROM walls ORDER BY surface_id").fetchall()),
        )
