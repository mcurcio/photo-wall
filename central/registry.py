"""Registry authority: observations cannot mutate desired Frame configuration."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import Mapping
from contextlib import contextmanager

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field, StrictBool, model_validator

from central.db import Database
from central.displays.model import Readiness
from central.fleet.acceptance_evidence import applied_is_latest
from central.fleet.device_fence import fence_in, retire_in
from central.fleet.models import TrialRefusal, TrialRefused
from central.fleet.position_history import (
    created_record,
    position_values,
    profile_changed_record,
    saved_record,
)
from central.frame_lighting.ports import FrameLightingCheck
from central.infra.display_store import (
    POSITION_COLUMNS,
    POSITION_FACTS,
    SCREEN_COLUMNS,
    SCREEN_FACTS,
    SCREEN_JOIN,
    SEEN_DISPLAY_JOIN,
    commit_position,
    frame_display_mm,
    frame_readiness,
    frame_readiness_in,
    frame_screen_px,
    resolve_output,
)
from central.infra.output_wakes import notify_nodes_in
from central.infra.position_history_store import record_created_in, write_position_in, write_turn_in
from central.infra.power_store import forget_frame_in
from central.infra.transactions import PgTransaction
from central.installation_models import (
    FrameInventory,
    InstallationInventory,
    Name,
    OutputInventory,
    PlayerInventory,
    WallInventory,
    WallNaming,
)
from central.transaction_locks import acquire_runtime_locks, holds_runtime_locks_in
from contracts.display_turn import DisplayTurn
from contracts.enrollment import Enrollment as Enrollment
from contracts.enrollment import OutputReport as OutputReport
from contracts.enrollment import enrollment_message as enrollment_message
from contracts.frame_lighting import FrameLighting
from contracts.models import (
    Calibration,
    ExactTurn,
    FrameProfile,
    Identifier,
    Model,
    OutputBinding,
    TargetIdentifier,
    off_screen,
)
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


class RegistryError(Exception):
    def __init__(self, code: str, status: int = 409, *, details: dict | None = None):
        self.code, self.status = code, status
        self.details = details or {}
        super().__init__(code)


def _orientation_coherent(width_mm: float, height_mm: float,
                          width_px: int, height_px: int) -> bool:
    """True when physical and pixel dimensions agree on orientation:
    height_mm == width_mm OR (height_mm > width_mm) == (height_px > width_px).
    Single copy of the invariant shared by FrameCreate and Registry.place_frame."""
    return height_mm == width_mm or (height_mm > width_mm) == (height_px > width_px)


class FrameCreate(Model):
    # A new Frame's id must be reachable by a Scene (contracts TARGET_ID_PATTERN).
    # Stored Frames and path parameters stay `Identifier`, so an older id is still
    # readable and deletable.
    id: TargetIdentifier
    name: Name | None = None              # None: "Frame N", N the smallest free
    surface_id: Identifier = "wall"
    x_mm: float = 0
    y_mm: float = 0
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    profile: FrameProfile

    @model_validator(mode="after")
    def oriented_profile(self):
        if not _orientation_coherent(self.width_mm, self.height_mm,
                                     self.profile.width_px, self.profile.height_px):
            raise ValueError("display profile must use dimensions oriented to the physical Frame")
        return self


class FramePlacement(Model):
    surface_id: Identifier | None = None
    x_mm: float | None = None
    y_mm: float | None = None
    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)


class FrameProfileReplacement(Model):
    profile: FrameProfile
    expected_generation: int = Field(ge=0)


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
    write that changes which Frame an Output carries, or a bound Frame's turn, calls it."""
    for player_id in dict.fromkeys(player_id for player_id in player_ids if player_id is not None):
        notify_nodes_in(conn, "SELECT device_id FROM players WHERE id=%s", (player_id,))


def _smallest_free(conn, table: str, word: str) -> str:
    """`word` and the smallest N >= 1 no row of `table` (frames or walls) is named, ignoring case."""
    row = conn.execute(
        f"SELECT min(n) AS n FROM generate_series(1, (SELECT count(*) + 1 FROM {table})) n "  # noqa: S608
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE lower(t.name) = lower(%s || ' ' || n))",
        (word,)).fetchone()
    return f"{word} {row['n']}"


def _ensure_wall(conn, surface_id: str) -> None:
    """A wall row for `surface_id`, named "Wall N" when it is new: every surface a Frame is placed
    on is a named wall."""
    if conn.execute("SELECT 1 FROM walls WHERE surface_id=%s", (surface_id,)).fetchone() is None:
        conn.execute("INSERT INTO walls(surface_id, name) VALUES (%s, %s) ON CONFLICT (surface_id) DO NOTHING",
                     (surface_id, _smallest_free(conn, "walls", "Wall")))


def _name_or(exc: UniqueViolation, code: str) -> str:
    """name_taken when `exc` is migration 091's case-insensitive Frame name index, else `code`."""
    return "name_taken" if exc.diag.constraint_name == "frames_name" else code


class Registry:
    IDENTIFY_OUTPUT_TTL_SECONDS = 15

    def __init__(self, db: Database, clock: Clock, *, lighting: FrameLightingCheck):
        """`lighting`: the Frame-lighting check every lighting write goes through (the composition
        root's PgFrameLighting)."""
        self.db, self.clock = db, clock
        self.lighting = lighting

    @contextmanager
    def _equipment_write(self):
        """Serialize equipment mutation after Coordination then Runtime locks."""
        with self.db.transaction() as conn:
            acquire_runtime_locks(conn)
            yield conn

    def _audit(self, conn, kind: str, subject: str, detail: dict | None = None):
        conn.execute("INSERT INTO audit_events(occurred_at,kind,subject,detail) VALUES(%s,%s,%s,%s)",
                     (self.clock.utc(), kind, subject, Jsonb(detail or {})))

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
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734118322)")
            conn.execute("DELETE FROM enrollment_nonces WHERE expires_at <= %s", (now,))
            # A bounded outstanding challenge per key avoids unbounded growth on ordinary retry.
            conn.execute("DELETE FROM enrollment_nonces WHERE public_key=%s", (public_key,))
            if conn.execute("SELECT count(*) AS n FROM enrollment_nonces").fetchone()["n"] >= 1024:
                raise RegistryError("enrollment_busy", 429)
            conn.execute("INSERT INTO enrollment_nonces VALUES(%s,%s,%s)",
                         (nonce, public_key, now + 60))
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
            conn.execute("SELECT pg_advisory_xact_lock(734118322)")
            challenge = conn.execute("DELETE FROM enrollment_nonces WHERE nonce=%s "
                                     "AND public_key=%s AND expires_at>%s RETURNING nonce",
                                     (request.nonce, request.public_key, now)).fetchone()
            if not challenge:
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
                             "authority_epoch=authority_epoch+1 WHERE id=%s",
                             (request.public_key, token_hash, now, player_id))
            else:
                conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,device_id) "
                             "VALUES(%s,%s,%s,%s,%s,%s)",
                             (player_id, request.public_key, token_hash, now, now,
                              request.device_id))
            conn.execute("UPDATE outputs SET observation=jsonb_set(observation,'{connected}','false') "
                         "WHERE player_id=%s", (player_id,))
            for output in request.outputs:
                conn.execute("INSERT INTO outputs VALUES(%s,%s,%s) ON CONFLICT(player_id,output_id) "
                             "DO UPDATE SET observation=excluded.observation",
                             (player_id, output.output_id, Jsonb(output.model_dump())))
            epoch = conn.execute("SELECT authority_epoch FROM players WHERE id=%s",
                                 (player_id,)).fetchone()["authority_epoch"]
            # Negotiation belongs to this enrollment epoch, never to the serial or package tag.
            self._reset_control(conn, player_id, epoch, "open")
            # Every Player enrolls unbound (pending) by serial alone; its boot is recorded.
            conn.execute("UPDATE players SET health=health || %s WHERE id=%s",
                         (Jsonb({"boot_id": request.boot_id}), player_id))
            self._audit(conn, "player_enrolled", player_id)
        return {"player_id": player_id, "token": token, "authority_epoch": epoch}

    def authenticate(self, token: str) -> dict:
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
    def _control_player(conn, player_id: str, epoch: int) -> None:
        row = conn.execute(
            "SELECT authority_epoch,retired_at FROM players WHERE id=%s FOR UPDATE",
            (player_id,),
        ).fetchone()
        if not row or row["retired_at"] is not None or row["authority_epoch"] != epoch:
            raise RegistryError("stale_authority", 403)

    @staticmethod
    def _reset_control(conn, player_id: str, epoch: int, status: str) -> None:
        conn.execute(
            "INSERT INTO player_control_sessions(player_id,authority_epoch,status) "
            "VALUES(%s,%s,%s) ON CONFLICT(player_id) DO UPDATE SET "
            "authority_epoch=EXCLUDED.authority_epoch,status=EXCLUDED.status,"
            "capabilities='[]',offered_schemas=NULL,offered_capabilities=NULL,"
            "issued_sequence=0,pending_id=NULL,pending_digest=NULL,pending_expires=NULL,"
            "applied_sequence=0,applied_at=NULL,applied_delivery_id=NULL,"
            "applied_digest=NULL,applied_ack_nonce=NULL,last_delivery_id=NULL,last_result_sequence=NULL,"
            "last_result_digest=NULL,last_result=NULL,last_result_at=NULL",
            (player_id, epoch, status),
        )

    def control_hello(self, player_id: str, request: ControlHello) -> ControlSelection:
        with self.db.transaction() as conn:
            self._control_player(conn, player_id, request.authority_epoch)
            row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                               (player_id,)).fetchone()
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
                self._reset_control(conn, player_id, request.authority_epoch, "open")
            conn.execute(
                "UPDATE player_control_sessions SET status='negotiated',"
                "capabilities=%s,offered_schemas=%s,offered_capabilities=%s "
                "WHERE player_id=%s",
                (Jsonb(list(selection.capabilities)),
                 Jsonb(list(request.schemas)),
                 Jsonb(list(request.capabilities)), player_id),
            )
            return selection

    def control_selection(self, player_id: str, epoch: int) -> ControlSelection:
        with self.db.transaction() as conn:
            return self.control_selection_in(conn, player_id, epoch)

    def control_selection_in(self, conn, player_id: str, epoch: int) -> ControlSelection:
        """The epoch's negotiated selection, read under a caller-owned Coordination
        transaction when issuing state. State is served only after the hello."""
        self._control_player(conn, player_id, epoch)
        row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                           (player_id,)).fetchone()
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
        """Issue under the caller's Coordinator lock, atomic with its projected state."""
        self._control_player(conn, player_id, epoch)
        now = self.clock.utc()
        row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                           (player_id,)).fetchone()
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
        issued = conn.execute(
            "UPDATE player_control_sessions SET issued_sequence=issued_sequence+1,"
            "pending_id=%s,pending_digest=%s,pending_expires=%s,"
            "applied_ack_nonce=NULL WHERE player_id=%s "
            "RETURNING issued_sequence",
            (delivery_id, digest, now + 60, player_id),
        ).fetchone()
        return ControlDelivery(delivery_id=delivery_id,
                               delivery_sequence=issued["issued_sequence"]).model_dump()

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
        with self.db.transaction() as conn:
            self._control_player(conn, player_id, report.authority_epoch)
            now = self.clock.utc()
            row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                               (player_id,)).fetchone()
            if (not row or row["authority_epoch"] != report.authority_epoch
                    or row["status"] != "negotiated"):
                return ControlAckResponse(accepted=False)
            if row["pending_id"] != report.delivery_id:
                if (report.result != "applied" and applied_is_latest(row)
                        and row["applied_delivery_id"] == report.delivery_id):
                    # The Player re-applied the re-sent delivery and it did not take: that is
                    # the latest result, so the receipt is no longer current and the next
                    # poll issues a fresh delivery. A partial application never counts.
                    conn.execute(
                        "UPDATE player_control_sessions SET last_result=%s,last_result_at=%s,"
                        "applied_ack_nonce=NULL WHERE player_id=%s",
                        (report.result, now, player_id))
                    return ControlAckResponse(accepted=True)
                receipt = self._current_applied_receipt(row, report)
                return ControlAckResponse(accepted=receipt is not None, receipt=receipt)
            if row["pending_expires"] is None or row["pending_expires"] <= now:
                return ControlAckResponse(accepted=False)
            # The nonce is generated only after the ACK passes the current-epoch,
            # pending-delivery and expiry checks. It is committed with the result.
            nonce = secrets.token_hex(32) if report.result == "applied" else None
            conn.execute(
                "UPDATE player_control_sessions SET pending_id=NULL,pending_digest=NULL,"
                "pending_expires=NULL,last_delivery_id=%s,last_result_sequence=issued_sequence,"
                "last_result_digest=pending_digest,last_result=%s,last_result_at=%s,"
                "applied_ack_nonce=%s,"
                "applied_sequence=CASE WHEN %s='applied' THEN issued_sequence "
                "ELSE applied_sequence END,"
                "applied_at=CASE WHEN %s='applied' THEN %s ELSE applied_at END,"
                "applied_delivery_id=CASE WHEN %s='applied' THEN pending_id "
                "ELSE applied_delivery_id END,"
                "applied_digest=CASE WHEN %s='applied' THEN pending_digest "
                "ELSE applied_digest END "
                "WHERE player_id=%s",
                (report.delivery_id, report.result, now, nonce,
                 report.result, report.result, now,
                 report.result, report.result, player_id),
            )
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

    def control_fact(self, player_id: str) -> dict | None:
        """Read provenance-labelled app-control history without readiness inference."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT c.authority_epoch,c.status,c.issued_sequence,"
                "c.pending_id,c.pending_digest,c.applied_sequence,c.applied_at,"
                "c.applied_delivery_id,c.applied_digest,c.last_delivery_id,"
                "c.last_result_sequence,c.last_result_digest,c.last_result,c.last_result_at "
                "FROM player_control_sessions c JOIN players p ON p.id=c.player_id "
                "AND p.authority_epoch=c.authority_epoch WHERE c.player_id=%s "
                "AND p.retired_at IS NULL",
                (player_id,),
            ).fetchone()
            return dict(row) if row else None

    def identify_output(self, player_id: str, output_id: str) -> dict:
        """Ask one active, connected, unbound Output to identify itself briefly."""
        now = self.clock.utc()
        request_id = uuid.uuid4()
        expires_at = now + self.IDENTIFY_OUTPUT_TTL_SECONDS
        with self._equipment_write() as conn:
            player = conn.execute(
                "SELECT authority_epoch FROM players WHERE id=%s AND retired_at IS NULL FOR UPDATE",
                (player_id,),
            ).fetchone()
            if not player:
                raise RegistryError("unknown_or_retired_player", 404)
            negotiated = conn.execute(
                "SELECT 1 FROM player_control_sessions WHERE player_id=%s "
                "AND authority_epoch=%s AND status='negotiated' "
                "AND capabilities ? 'identify_output'",
                (player_id, player["authority_epoch"]),
            ).fetchone()
            if not negotiated:
                raise RegistryError("identify_unsupported")
            output = conn.execute(
                "SELECT observation FROM outputs WHERE player_id=%s AND output_id=%s FOR UPDATE",
                (player_id, output_id),
            ).fetchone()
            if not output:
                raise RegistryError("unknown_output", 404)
            if not output["observation"].get("connected", False):
                raise RegistryError("output_disconnected")
            if conn.execute("SELECT 1 FROM bindings WHERE player_id=%s AND output_id=%s",
                            (player_id, output_id)).fetchone():
                raise RegistryError("output_bound")
            conn.execute(
                "INSERT INTO player_output_identification(player_id,request_id,output_id,"
                "authority_epoch,created_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(player_id) DO UPDATE SET request_id=EXCLUDED.request_id,"
                "output_id=EXCLUDED.output_id,authority_epoch=EXCLUDED.authority_epoch,"
                "created_at=EXCLUDED.created_at,expires_at=EXCLUDED.expires_at",
                (player_id, request_id, output_id, player["authority_epoch"], now, expires_at),
            )
            self._audit(conn, "output_identification_requested", player_id,
                        {"request_id": str(request_id), "output_id": output_id,
                         "authority_epoch": player["authority_epoch"]})
        return {"request_id": str(request_id), "output_id": output_id,
                "expires_at": expires_at}

    def create_frame(self, frame: FrameCreate) -> dict:
        """A new Frame at the default Position (`Calibration()`: centred, the screen's full width) and the lighting a new Frame starts with (a new Frame is bound
        to no port yet, so no Display is seen: UPGRADE_DISPLAY_LIGHT), and its KEPT_CREATED Position
        entry, in one transaction."""
        try:
            with self.db.transaction() as conn:
                name = frame.name if frame.name is not None else _smallest_free(conn, "frames", "Frame")
                calibration = Calibration()
                lighting = self.lighting.new_frame_within(PgTransaction(conn), UPGRADE_DISPLAY_LIGHT)
                conn.execute("INSERT INTO frames(id,name,surface_id,x_mm,y_mm,width_mm,height_mm,"
                             "profile,video,calibration,lighting) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                             (frame.id, name, frame.surface_id, frame.x_mm, frame.y_mm,
                              frame.width_mm, frame.height_mm, Jsonb(frame.profile.model_dump()),
                              frame.profile.video,   # transitional: fb-14 takes video from FrameCreate
                              Jsonb(calibration.model_dump(mode="json")), Jsonb(lighting.model_dump(mode="json"))))
                record_created_in(conn, frame.id, created_record(position_values(calibration, lighting)))
                _ensure_wall(conn, frame.surface_id)
                self._audit(conn, "frame_created", frame.id)
        except UniqueViolation as exc:
            raise RegistryError(_name_or(exc, "frame_exists")) from exc
        return {**frame.model_dump(), "name": name}

    def rename_frame(self, frame_id: str, name: str) -> dict:
        """Name a Frame (#94). RegistryError unknown_frame 404, name_taken 409 (ignoring case)."""
        try:
            with self.db.transaction() as conn:
                row = conn.execute("UPDATE frames SET name=%s WHERE id=%s RETURNING id",
                                   (name, frame_id)).fetchone()
                if row is None:
                    raise RegistryError("unknown_frame", 404)
                self._audit(conn, "frame_renamed", frame_id, {"name": name})
        except UniqueViolation as exc:
            raise RegistryError(_name_or(exc, "frame_exists")) from exc
        return {"frame_id": frame_id, "name": name}

    def set_frame_video(self, frame_id: str, video: bool) -> dict:
        """Whether the Frame shows videos (frames.video): {"frame_id", "video", "changed"}.
        RegistryError unknown_frame 404. Last write wins: no generation, configuration revision or
        Position check (it changes planning, not the picture), so a ready Frame stays ready; audits
        "frame_video_changed" {"video"} when it changes. The Planner reads it at its next plan, as
        it reads a saved photo-fit setting (nothing to wake). Until fb-14 drops the Frame profile,
        the profile's "video" is written with it (transitional)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT video FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
            if row is None:
                raise RegistryError("unknown_frame", 404)
            if row["video"] == video:
                return {"frame_id": frame_id, "video": video, "changed": False}
            conn.execute("UPDATE frames SET video=%s, profile=jsonb_set(profile, '{video}', to_jsonb(%s::boolean)) "
                         "WHERE id=%s", (video, video, frame_id))
            self._audit(conn, "frame_video_changed", frame_id, {"video": video})
        return {"frame_id": frame_id, "video": video, "changed": True}

    def name_wall(self, surface_id: str, naming: WallNaming) -> WallInventory:
        """Name a wall and label its room (#92). RegistryError unknown_wall 404."""
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE walls SET name=%s, room=%s WHERE surface_id=%s "
                               "RETURNING surface_id, name, room",
                               (naming.name, naming.room, surface_id)).fetchone()
            if row is None:
                raise RegistryError("unknown_wall", 404)
            self._audit(conn, "wall_named", surface_id, naming.model_dump())
        return WallInventory.model_validate(row)

    def frame_profile(self, frame_id: str) -> FrameProfile:
        """Read the persistent profile for a Frame, whether it is bound yet."""
        with self.db.transaction() as conn:
            return self.frame_profiles_in(conn, (frame_id,))[frame_id]

    def frame_profiles_in(self, conn, frame_ids) -> dict[str, FrameProfile]:
        """Read all requested Frame profiles in the caller's transaction."""
        ids = tuple(dict.fromkeys(frame_ids))
        if not ids:
            return {}
        rows = conn.execute("SELECT id,profile FROM frames WHERE id=ANY(%s)", (list(ids),)).fetchall()
        profiles = {row["id"]: FrameProfile.model_validate(row["profile"]) for row in rows}
        if len(profiles) != len(ids):
            raise RegistryError("unknown_frame", 404)
        return profiles

    def bind(self, frame_id: str, player_id: str, output_id: str, *, expected_generation: int) -> dict:
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
                if not conn.execute("SELECT 1 FROM outputs WHERE player_id=%s AND output_id=%s",
                                    (player_id, output_id)).fetchone():
                    raise RegistryError("unknown_output", 404)
                existing = conn.execute("SELECT * FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone()
                if existing and (existing["player_id"], existing["output_id"]) == (player_id, output_id):
                    return {"generation": frame["generation"], "changed": False}
                if frame["generation"] != expected_generation:
                    raise RegistryError("binding_generation_conflict")
                conn.execute("DELETE FROM bindings WHERE frame_id=%s", (frame_id,))
                conn.execute("INSERT INTO bindings VALUES(%s,%s,%s)", (frame_id, player_id, output_id))
                # The Output now carries this Frame (its timetable and turn), and the one it left
                # carries none: wake both Nodes' document sources, on commit.
                _wake_nodes_of(conn, player_id, None if existing is None else existing["player_id"])
                resolve_output(conn, player_id, output_id)   # the display there, under this binding
                row = conn.execute("UPDATE frames SET generation=generation+1,"
                                   "configuration_revision=configuration_revision+1 "
                                   "WHERE id=%s RETURNING generation",
                                   (frame_id,)).fetchone()
                self._audit(conn, "frame_bound", frame_id,
                            {"player_id": player_id, "output_id": output_id, **row})
                return {**row, "changed": True}
        except UniqueViolation as exc:
            raise RegistryError("output_already_bound") from exc

    def unbind(self, frame_id: str, *, expected_generation: int) -> dict:
        """Release a Frame's active binding, reversibly: unlike retire, the player record
        (and its ability to be re-bound, to this or another Frame) is untouched."""
        with self._equipment_write() as conn:
            frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            existing = conn.execute("SELECT * FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone()
            if not existing:
                raise RegistryError("not_bound", 404)
            if frame["generation"] != expected_generation:
                raise RegistryError("binding_generation_conflict")
            conn.execute("DELETE FROM bindings WHERE frame_id=%s", (frame_id,))
            _wake_nodes_of(conn, existing["player_id"])   # its Output now carries no Frame (turn 0)
            row = conn.execute("UPDATE frames SET generation=generation+1,"
                               "configuration_revision=configuration_revision+1 "
                               "WHERE id=%s RETURNING generation",
                               (frame_id,)).fetchone()
            self._audit(conn, "frame_unbound", frame_id,
                        {"player_id": existing["player_id"], "output_id": existing["output_id"], **row})
            return {**row, "changed": True}

    def place_frame(self, frame_id: str, placement: FramePlacement) -> dict:
        """Reposition: last-write-wins, no token (the one deliberate exception to R3,
        design §9a). FOR UPDATE makes the read-merge-write atomic; the row is still LWW.
        Bumps NO generation, NO configuration_revision; never touches calibration."""
        with self._equipment_write() as conn:
            frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE",
                                 (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            merged = {field: value if value is not None else frame[field]
                      for field, value in placement.model_dump().items()}
            profile = FrameProfile.model_validate(frame["profile"])
            if not _orientation_coherent(merged["width_mm"], merged["height_mm"],
                                         profile.width_px, profile.height_px):
                raise RegistryError("oriented_profile", 422)
            conn.execute("UPDATE frames SET surface_id=%s,x_mm=%s,y_mm=%s,width_mm=%s,"
                         "height_mm=%s WHERE id=%s",
                         (merged["surface_id"], merged["x_mm"], merged["y_mm"],
                          merged["width_mm"], merged["height_mm"], frame_id))
            _ensure_wall(conn, merged["surface_id"])
            self._audit(conn, "frame_repositioned", frame_id)
            return {"id": frame_id, **merged}

    def turn_frame(self, frame_id: str, turn: DisplayTurn, *, expected_generation: int) -> dict:
        """Set how the Frame's screen hangs (frames.turn), in one equipment write: RegistryError
        unknown_frame 404, then binding_generation_conflict 409 when `expected_generation` is not the
        Frame's (the token before idempotency, as `replace_frame_profile`); the same turn answers
        {"frame_id", "turn", "generation", "changed": False} with no write. A new turn raises the
        generation and configuration_revision (a Position made for the other hanging needs checking:
        readiness POSITION_NEEDED until the next Save) and appends the kept-turn-changed Position history
        entry (`write_turn_in`: the Position as it is, made for the new turn, so Restore refuses the
        entries made for the old one), audits "frame_turned" {"turn", "generation"}
        and wakes the bound Player's Node's document source (`notify_nodes_in`), so its Output
        document carries the turn without waiting for a deadline; answers {..., "changed": True}.
        Never touches the Position, the binding or the lighting."""
        with self._equipment_write() as conn:
            frame = conn.execute("SELECT generation, turn FROM frames WHERE id=%s FOR UPDATE",
                                 (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            if frame["generation"] != expected_generation:
                raise RegistryError("binding_generation_conflict")
            if frame["turn"] == turn:
                return {"frame_id": frame_id, "turn": turn, "generation": frame["generation"],
                        "changed": False}
            row = {"generation": write_turn_in(conn, frame_id, turn)}
            self._audit(conn, "frame_turned", frame_id, {"turn": turn, **row})
            bound = conn.execute("SELECT player_id FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone()
            if bound is not None:
                _wake_nodes_of(conn, bound["player_id"])
            return {"frame_id": frame_id, "turn": turn, **row, "changed": True}

    def replace_frame_profile(self, frame_id: str, profile: FrameProfile, *,
                              expected_generation: int, conn) -> dict:
        """Replace display dimensions after an optimistic generation check.

        The persistent Frame and its placement remain the same. A changed display
        profile invalidates committed calibration. A change is a kept Position write
        (configuration snapshots, migration 096): its kept-profile-changed entry, made for the new
        profile, follows any change made outside Photo Wall recorded first.
        """
        if not holds_runtime_locks_in(conn):
            raise RegistryError("frame_runtime_snapshot_required", 500)
        frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
        if not frame:
            raise RegistryError("unknown_frame", 404)
        # Check the token before idempotency so an old tab cannot silently
        # succeed merely because another operator chose the same profile.
        if frame["generation"] != expected_generation:
            raise RegistryError("binding_generation_conflict")
        current = FrameProfile.model_validate(frame["profile"])
        if current == profile:
            return {"profile": current.model_dump(), "generation": frame["generation"],
                    "changed": False}
        # A bound Frame's profile is the Position's ground truth; it opens only while the Display
        # on its Output is not the one its Position names (the display-changed card, slice C2).
        if frame_readiness_in(conn, frame_id) not in (Readiness.UNBOUND, Readiness.DISPLAY_CHANGED):
            raise RegistryError("frame_bound")
        if not _orientation_coherent(frame["width_mm"], frame["height_mm"],
                                     profile.width_px, profile.height_px):
            raise RegistryError("oriented_profile", 422)
        calibration = Calibration.model_validate(frame["calibration"])
        invalidated = calibration.model_copy(update={"revision": calibration.revision + 1})
        row = {"generation": write_position_in(
            conn, frame_id, profile_changed_record(position_values(calibration, FrameLighting.model_validate(frame["lighting"]))),
            calibration=invalidated, profile=profile)}
        # Transitional (fb-14 deletes): the profile's video is the Frame's Shows videos too.
        conn.execute("UPDATE frames SET video=%s WHERE id=%s", (profile.video, frame_id))
        self._audit(conn, "frame_profile_changed", frame_id,
                    {"profile": profile.model_dump(), **row})
        return {"profile": profile.model_dump(), **row, "changed": True}

    def delete_frame(self, frame_id: str, *, conn, references: Mapping[str, tuple[str, ...]]) -> dict:
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
        if conn.execute("SELECT 1 FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone():
            raise RegistryError("frame_bound")
        if references.get("scene_ids") or references.get("queued_activation_ids"):
            raise RegistryError("frame_referenced", 409, details={
                "scene_ids": list(references.get("scene_ids", ())),
                "program_ids": list(references.get("program_ids", ())),
                "queued_activation_ids": list(references.get("queued_activation_ids", ())),
            })
        conn.execute("DELETE FROM frames WHERE id=%s", (frame_id,))
        forget_frame_in(conn, frame_id)
        self._audit(conn, "frame_deleted", frame_id)
        return {"status": "deleted"}

    def retire(self, player_id: str) -> None:
        """Retire a Player permanently. Refuses (409 player_bound), for every caller,
        while any of its Outputs is bound: replacement is unbind, then retire.

        The bindings check runs AFTER the Player row lock, which bind takes first
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
            if conn.execute("SELECT 1 FROM bindings WHERE player_id=%s LIMIT 1",
                            (player_id,)).fetchone():
                raise RegistryError("player_bound")
            # A Player that never booted from a node offer (a Compose or demo Player) has no
            # `devices` row; retirement writes its canonical tombstone.
            retire_in(fenced, now)
            conn.execute("UPDATE players SET retired_at=%s,authority_epoch=authority_epoch+1 "
                         "WHERE id=%s", (now, player_id))
            self._audit(conn, "player_retired", player_id)

    @staticmethod
    def calibration_mode_in(conn, frame_id: str) -> str:
        """`native_trial` when the Frame's bound Player runs on a Node Central admitted (its
        device holds a node boot admission, current or past); otherwise `not_admitted`: a Player
        that never booted from a node offer (a Compose or demo Player) has no calibration
        protocol. A revoked admission does not withdraw the capability."""
        row = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM bindings b JOIN players p ON p.id=b.player_id "
            "JOIN node_boot_admissions a ON a.device_id=p.device_id "
            "WHERE b.frame_id=%s) AS native",
            (frame_id,),
        ).fetchone()
        return "native_trial" if row["native"] else "not_admitted"

    def calibration_capability(self, frame_id: str) -> dict:
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM frames WHERE id=%s", (frame_id,)).fetchone():
                raise RegistryError("unknown_frame", 404)
            return {"mode": self.calibration_mode_in(conn, frame_id)}

    def commit_calibration_trial_in(self, conn, *, frame_id: str, player_id: str,
            authority_epoch: int, output_id: str, binding_generation: int,
            config_revision: int, calibration_revision: int,
            calibration: Calibration, lighting: FrameLighting, session_id: uuid.UUID | None,
            restored_from: uuid.UUID | None) -> Calibration:
        """Registry-owned Save CAS inside the Trial owner's existing transaction.

        Caller holds Coordination→Runtime, then the device fence and session locks. This method
        owns the persistent Frame calibration and lighting (the lighting rides the binding beside the
        calibration and is saved with it; it never changes the Frame output). It never interprets
        trial evidence. It is a kept Position write: a change made outside Photo Wall is recorded
        first, then the Save's kept-saved entry
        (the trial's `session_id`, and `restored_from` when its draft came from a Restore), in this
        transaction. A Rectangle that runs off its screen as it hangs (`contracts.models.off_screen`
        on the bound Output's reported size, `outputs.observation` as built, and the Frame's turn)
        is refused with POSITION_OFF_SCREEN before any write; an Output that reported no size is
        not checked.
        """
        if not holds_runtime_locks_in(conn):
            raise RegistryError("calibration_runtime_locks_required")
        row = conn.execute("SELECT f.*,p.authority_epoch,p.retired_at,b.player_id,b.output_id,"
                           "(o.observation->>'width_px')::int AS output_width_px,"
                           "(o.observation->>'height_px')::int AS output_height_px "
                           "FROM frames f JOIN bindings b ON b.frame_id=f.id "
                           "JOIN players p ON p.id=b.player_id "
                           "LEFT JOIN outputs o ON o.player_id=b.player_id AND o.output_id=b.output_id "
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
        width, height = row["output_width_px"], row["output_height_px"]
        size = (width, height) if width is not None and height is not None else None
        if off_screen(FrameProfile.model_validate(row["profile"]), calibration, size, row["turn"]):
            raise TrialRefused(TrialRefusal.POSITION_OFF_SCREEN)
        committed = Calibration.model_validate(calibration.model_dump()).model_copy(
            update={"revision": current.revision + 1})
        write_position_in(conn, frame_id, saved_record(position_values(committed, lighting), session_id=session_id,
                                                       restored_from=restored_from), calibration=committed)
        commit_position(conn, frame_id)
        self._audit(conn, "calibration_trial_commit", frame_id)
        return committed

    def bindings_for(self, player_id: str, epoch: int, *, include_unvalidated=False) -> list[OutputBinding]:
        config = self.configuration_for(player_id, epoch)
        return config["bindings" if include_unvalidated else "execution_bindings"]

    def configuration_for(self, player_id: str, epoch: int) -> dict[str, list[OutputBinding]]:
        with self.db.transaction() as conn:
            return self.configuration_in(conn, player_id, epoch)

    def configuration_in(self, conn, player_id: str, epoch: int) -> dict[str, list[OutputBinding]]:
        """Hold registry authority through the caller's offer/configuration transaction."""
        player = conn.execute("SELECT 1 FROM players WHERE id=%s AND authority_epoch=%s "
                              "AND retired_at IS NULL FOR SHARE", (player_id, epoch)).fetchone()
        if not player:
            raise RegistryError("stale_authority", 403)
        rows = conn.execute(f"SELECT f.*,b.output_id,{POSITION_COLUMNS} FROM bindings b "  # noqa: S608
                            f"JOIN frames f ON f.id=b.frame_id {SEEN_DISPLAY_JOIN} "
                            "WHERE b.player_id=%s ORDER BY b.output_id FOR SHARE OF f,b",
                            (player_id,)).fetchall()
        bindings = [OutputBinding(output_id=r["output_id"], frame_id=r["id"], generation=r["generation"],
                              configuration_revision=r["configuration_revision"],
                              profile=FrameProfile.model_validate(r["profile"]),
                              calibration=Calibration.model_validate(r["calibration"]),
                              lighting=FrameLighting.model_validate(r["lighting"]))
                for r in rows]
        return {"bindings": bindings, "execution_bindings": [binding for binding, row in
                zip(bindings, rows, strict=True) if frame_readiness(row) is Readiness.READY]}

    def inventory(self) -> InstallationInventory:
        with self.db.transaction() as conn:
            return self.inventory_in(conn, self.clock.utc())

    def inventory_in(self, conn, now: float) -> InstallationInventory:
        """Read inventory in a caller-owned transaction; safe in read-only snapshots."""
        players = conn.execute("SELECT id,device_id,authority_epoch,registered_at,last_seen,retired_at,health "
                               "FROM players ORDER BY registered_at,id").fetchall()
        outputs = conn.execute("SELECT player_id,output_id,observation FROM outputs "
                               "ORDER BY player_id,output_id").fetchall()
        frames = conn.execute("SELECT f.id,f.name,f.surface_id,f.x_mm,f.y_mm,f.width_mm,f.height_mm,f.profile,f.video,"  # noqa: S608
                              "f.calibration,f.lighting,f.turn,f.configuration_revision,"
                              f"b.player_id,b.output_id,{POSITION_COLUMNS},{SCREEN_COLUMNS} FROM frames f "
                              f"LEFT JOIN bindings b ON b.frame_id=f.id {SCREEN_JOIN} ORDER BY f.id").fetchall()
        bound_player_ids = {frame["player_id"] for frame in frames if frame["player_id"] is not None}
        return InstallationInventory(
            players=tuple(PlayerInventory.model_validate({**row, "is_bound": row["id"] in bound_player_ids})
                         for row in players),
            outputs=tuple(OutputInventory.model_validate(row) for row in outputs),
            frames=tuple(FrameInventory.model_validate(
                {**{key: value for key, value in row.items() if key not in POSITION_FACTS + SCREEN_FACTS},
                 "generation": row["generation"], "readiness": frame_readiness(row),
                 "screen_px": frame_screen_px(row), "display_mm": frame_display_mm(row)}) for row in frames),
            walls=tuple(WallInventory.model_validate(row) for row in conn.execute(
                "SELECT surface_id, name, room FROM walls ORDER BY surface_id").fetchall()),
        )
