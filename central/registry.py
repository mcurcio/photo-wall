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
from pydantic import Field, model_validator

from central.db import Database
from central.fleet.locks import lock_fleet_assets_in
from central.installation_models import (
    FrameInventory,
    InstallationInventory,
    OutputInventory,
    PlayerInventory,
)
from central.transaction_locks import acquire_runtime_locks, holds_runtime_locks_in
from contracts.enrollment import Enrollment as Enrollment
from contracts.enrollment import OutputReport as OutputReport
from contracts.enrollment import enrollment_message as enrollment_message
from contracts.models import (
    Calibration,
    FrameProfile,
    Identifier,
    Model,
    OutputBinding,
    TargetIdentifier,
)
from contracts.player_control import (
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


class Registry:
    IDENTIFY_OUTPUT_TTL_SECONDS = 15

    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    @contextmanager
    def _equipment_write(self):
        """Serialize equipment mutation after Coordination then Runtime locks."""
        with self.db.transaction() as conn:
            acquire_runtime_locks(conn)
            yield conn

    def _audit(self, conn, kind: str, subject: str, detail: dict | None = None):
        conn.execute("INSERT INTO audit_events(occurred_at,kind,subject,detail) VALUES(%s,%s,%s,%s)",
                     (self.clock.utc(), kind, subject, Jsonb(detail or {})))

    def _expire_previews(self, conn):
        conn.execute("UPDATE frames SET preview=NULL,preview_expires=NULL,"
                     "configuration_revision=configuration_revision+1 "
                     "WHERE preview_expires <= %s", (self.clock.utc(),))

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
                                              request.boot_id, request.ticket_id)
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
            # Serial enrollment has no OS command authority. It must still
            # respect permanent device retirement while an offer or retirement
            # transaction may be creating/updating the canonical row.
            lock_fleet_assets_in(conn)
            device = conn.execute("SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE",
                                  (request.device_id,)).fetchone()
            if device is not None and device["retired_at"] is not None:
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
            # Ticketless enroll (0009): the signed-rootfs boot-ticket path has
            # been retired, so no boot server ever issues a ticket and there is
            # no release session to bind. Every player enrolls unbound (pending)
            # by serial alone; request.ticket_id is always None now and is only
            # recorded in health below.
            conn.execute("UPDATE players SET health=health || %s WHERE id=%s",
                         (Jsonb({"boot_id": request.boot_id, "ticket_id": request.ticket_id}),
                          player_id))
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
            "authority_epoch=EXCLUDED.authority_epoch,status=EXCLUDED.status,schema_version=1,"
            "capabilities='[]',offered_schemas=NULL,offered_capabilities=NULL,"
            "issued_sequence=0,pending_id=NULL,pending_digest=NULL,pending_expires=NULL,"
            "applied_sequence=0,applied_at=NULL,applied_delivery_id=NULL,"
            "applied_digest=NULL,applied_ack_nonce=NULL,last_delivery_id=NULL,last_result_sequence=NULL,"
            "last_result_digest=NULL,last_result=NULL,last_result_at=NULL",
            (player_id, epoch, status),
        )

    @classmethod
    def _seal_legacy_control(cls, conn, player_id: str, epoch: int) -> ControlSelection:
        """An epoch enrolled by an older Central is already legacy on the wire."""
        cls._reset_control(conn, player_id, epoch, "legacy")
        return ControlSelection(authority_epoch=epoch, schema=1)

    def control_hello(self, player_id: str, request: ControlHello) -> ControlSelection:
        with self.db.transaction() as conn:
            self._control_player(conn, player_id, request.authority_epoch)
            row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                               (player_id,)).fetchone()
            if not row or row["authority_epoch"] != request.authority_epoch:
                # A mixed-version pod may have enrolled this current epoch without a
                # control row. Seal it as legacy so a new Player can still proceed.
                if 1 not in request.schemas:
                    raise RegistryError("control_schema_unsupported")
                return self._seal_legacy_control(conn, player_id, request.authority_epoch)
            if row["status"] == "negotiated":
                if (row["offered_schemas"] != list(request.schemas)
                        or row["offered_capabilities"] != list(request.capabilities)):
                    raise RegistryError("control_negotiation_conflict")
                return ControlSelection(authority_epoch=request.authority_epoch,
                                        schema=row["schema_version"],
                                        capabilities=tuple(row["capabilities"]))
            if row["status"] != "open":
                raise RegistryError("control_negotiation_closed")
            selection = select_control(request)
            if selection is None:
                raise RegistryError("control_schema_unsupported")
            conn.execute(
                "UPDATE player_control_sessions SET status='negotiated',schema_version=%s,"
                "capabilities=%s,offered_schemas=%s,offered_capabilities=%s "
                "WHERE player_id=%s",
                (selection.schema_version, Jsonb(list(selection.capabilities)),
                 Jsonb(list(request.schemas)),
                 Jsonb(list(request.capabilities)), player_id),
            )
            return selection

    def control_selection(self, player_id: str, epoch: int) -> ControlSelection:
        with self.db.transaction() as conn:
            return self.control_selection_in(conn, player_id, epoch)

    def control_selection_in(self, conn, player_id: str, epoch: int) -> ControlSelection:
        """Seal/select under a caller-owned Coordination transaction when issuing state."""
        self._control_player(conn, player_id, epoch)
        row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                           (player_id,)).fetchone()
        if not row or row["authority_epoch"] != epoch:
            return self._seal_legacy_control(conn, player_id, epoch)
        if row["status"] == "open":
            conn.execute("UPDATE player_control_sessions SET status='legacy' "
                         "WHERE player_id=%s", (player_id,))
            return ControlSelection(authority_epoch=epoch, schema=1)
        return ControlSelection(authority_epoch=epoch, schema=row["schema_version"],
                                capabilities=tuple(row["capabilities"]))

    def issue_control_delivery_record(self, player_id: str, epoch: int,
                                      digest: str) -> dict:
        """Persist a v2 challenge and sequence before sending either state transport."""
        with self.db.transaction() as conn:
            return self.issue_control_delivery_record_in(conn, player_id, epoch, digest)

    def issue_control_delivery_record_in(self, conn, player_id: str, epoch: int,
                                         digest: str) -> dict:
        """Issue under the caller's Coordinator lock, atomic with its projected state."""
        self._control_player(conn, player_id, epoch)
        now = self.clock.utc()
        row = conn.execute("SELECT * FROM player_control_sessions WHERE player_id=%s",
                           (player_id,)).fetchone()
        if not row or row["authority_epoch"] != epoch or row["schema_version"] != 2:
            raise RegistryError("control_protocol_mismatch")
        if (row["pending_id"] and row["pending_digest"] == digest
                and row["pending_expires"] > now):
            return ControlDelivery(delivery_id=row["pending_id"],
                                   delivery_sequence=row["issued_sequence"]).model_dump()
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

    def issue_control_delivery(self, player_id: str, epoch: int, digest: str) -> str:
        """Compatibility wrapper while HTTP and WebSocket adopt the sequence field."""
        return self.issue_control_delivery_record(player_id, epoch, digest)["delivery_id"]

    def control_ack(self, player_id: str, report: ControlAck) -> bool:
        """Compatibility wrapper for callers that only need ACK acceptance."""
        return self.control_ack_response(player_id, report).accepted

    @staticmethod
    def _current_applied_receipt(row: dict, report: ControlAck) -> ControlAppliedReceipt | None:
        """Replay only a receipt for the exact still-current applied delivery."""
        if (report.result != "applied" or row["status"] != "negotiated"
                or row["schema_version"] != 2
                or row["pending_id"] is not None or row["pending_digest"] is not None
                or row["pending_expires"] is not None
                or row["issued_sequence"] != row["applied_sequence"]
                or row["last_result_sequence"] != row["applied_sequence"]
                or row["last_result"] != "applied"
                or row["applied_at"] is None or row["last_result_at"] is None
                or row["applied_delivery_id"] != report.delivery_id
                or row["last_delivery_id"] != report.delivery_id
                or row["last_result_digest"] != row["applied_digest"]
                or row["applied_ack_nonce"] is None):
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
                    or row["schema_version"] != 2):
                return ControlAckResponse(accepted=False)
            if row["pending_id"] != report.delivery_id:
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
                "SELECT c.authority_epoch,c.schema_version,c.status,c.issued_sequence,"
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
            from central.equipment_drain import require_unfenced_player_in

            require_unfenced_player_in(conn, player_id)
            player = conn.execute(
                "SELECT authority_epoch FROM players WHERE id=%s AND retired_at IS NULL FOR UPDATE",
                (player_id,),
            ).fetchone()
            if not player:
                raise RegistryError("unknown_or_retired_player", 404)
            negotiated = conn.execute(
                "SELECT 1 FROM player_control_sessions WHERE player_id=%s "
                "AND authority_epoch=%s AND status='negotiated' AND schema_version=2 "
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
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO frames(id,surface_id,x_mm,y_mm,width_mm,height_mm,"
                             "profile,calibration) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                             (frame.id, frame.surface_id, frame.x_mm, frame.y_mm,
                              frame.width_mm, frame.height_mm, Jsonb(frame.profile.model_dump()),
                              Jsonb(Calibration().model_dump())))
                self._audit(conn, "frame_created", frame.id)
        except UniqueViolation as exc:
            raise RegistryError("frame_exists") from exc
        return frame.model_dump()

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
                from central.equipment_drain import (
                    require_unfenced_frame_in,
                    require_unfenced_player_in,
                )

                require_unfenced_player_in(conn, player_id)
                require_unfenced_frame_in(conn, frame_id)
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
                row = conn.execute("UPDATE frames SET generation=generation+1,calibration_valid=false,"
                                   "configuration_revision=configuration_revision+1,"
                                   "preview=NULL,preview_expires=NULL WHERE id=%s RETURNING generation",
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
            from central.equipment_drain import require_unfenced_frame_in

            require_unfenced_frame_in(conn, frame_id)
            frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            existing = conn.execute("SELECT * FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone()
            if not existing:
                raise RegistryError("not_bound", 404)
            if frame["generation"] != expected_generation:
                raise RegistryError("binding_generation_conflict")
            conn.execute("DELETE FROM bindings WHERE frame_id=%s", (frame_id,))
            row = conn.execute("UPDATE frames SET generation=generation+1,calibration_valid=false,"
                               "configuration_revision=configuration_revision+1,"
                               "preview=NULL,preview_expires=NULL WHERE id=%s RETURNING generation",
                               (frame_id,)).fetchone()
            self._audit(conn, "frame_unbound", frame_id,
                        {"player_id": existing["player_id"], "output_id": existing["output_id"], **row})
            return {**row, "changed": True}

    def place_frame(self, frame_id: str, placement: FramePlacement) -> dict:
        """Reposition: last-write-wins, no token (the one deliberate exception to R3,
        design §9a). FOR UPDATE makes the read-merge-write atomic; the row is still LWW.
        Bumps NO generation, NO configuration_revision; never touches calibration."""
        with self._equipment_write() as conn:
            from central.equipment_drain import require_unfenced_frame_in

            require_unfenced_frame_in(conn, frame_id)
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
            self._audit(conn, "frame_repositioned", frame_id)
            return {"id": frame_id, **merged}

    def replace_frame_profile(self, frame_id: str, profile: FrameProfile, *,
                              expected_generation: int, conn) -> dict:
        """Replace display dimensions after an optimistic generation check.

        The persistent Frame and its placement remain the same. A changed display
        profile invalidates committed calibration and any outstanding preview.
        """
        if not holds_runtime_locks_in(conn):
            raise RegistryError("frame_runtime_snapshot_required", 500)
        from central.equipment_drain import require_unfenced_frame_in

        require_unfenced_frame_in(conn, frame_id)
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
        if conn.execute("SELECT 1 FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone():
            raise RegistryError("frame_bound")
        if not _orientation_coherent(frame["width_mm"], frame["height_mm"],
                                     profile.width_px, profile.height_px):
            raise RegistryError("oriented_profile", 422)
        calibration = Calibration.model_validate(frame["calibration"])
        invalidated = calibration.model_copy(update={"revision": calibration.revision + 1})
        row = conn.execute(
            "UPDATE frames SET profile=%s,calibration=%s,calibration_valid=false,"
            "preview=NULL,preview_expires=NULL,generation=generation+1,"
            "configuration_revision=configuration_revision+1 WHERE id=%s "
            "RETURNING generation",
            (Jsonb(profile.model_dump()), Jsonb(invalidated.model_dump()), frame_id),
        ).fetchone()
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
        from central.equipment_drain import require_unfenced_frame_in

        require_unfenced_frame_in(conn, frame_id)
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
            from central.equipment_drain import require_unfenced_player_in
            from central.fleet.attempts import release_queued_for_device_in
            from central.fleet.fallback import retire_device_fallback_references

            require_unfenced_player_in(conn, player_id)
            identity = conn.execute("SELECT device_id FROM players WHERE id=%s",
                                    (player_id,)).fetchone()
            if identity is None:
                raise RegistryError("unknown_player", 404)
            device_id = identity["device_id"]
            # Fleet offer, fallback reservation and eviction take this lock
            # before the device row. Use the same order for retirement so a
            # fallback cannot be pinned between revocation and GC release.
            lock_fleet_assets_in(conn)
            now = self.clock.utc()
            # Ticketless legacy Players may predate any PXE `devices` row.
            # Operator retirement is allowed to create their canonical tombstone.
            conn.execute("INSERT INTO devices(device_id,first_seen,last_seen) "
                         "VALUES(%s,%s,%s) ON CONFLICT(device_id) DO NOTHING",
                         (device_id, now, now))
            conn.execute("SELECT retired_at FROM devices WHERE device_id=%s FOR UPDATE",
                         (device_id,)).fetchone()
            player = conn.execute("SELECT * FROM players WHERE id=%s FOR UPDATE", (player_id,)).fetchone()
            if player["retired_at"] is not None:
                return
            if conn.execute("SELECT 1 FROM bindings WHERE player_id=%s LIMIT 1",
                            (player_id,)).fetchone():
                raise RegistryError("player_bound")
            conn.execute("UPDATE devices SET retired_at=COALESCE(retired_at,%s) "
                         "WHERE device_id=%s", (now, device_id))
            conn.execute("UPDATE fleet_device_lifecycle SET generation=generation+1,"
                         "revoked_at=%s WHERE device_id=%s AND revoked_at IS NULL",
                         (now, device_id))
            conn.execute("UPDATE fleet_os_command_sessions SET revoked_at=%s "
                         "WHERE device_id=%s AND revoked_at IS NULL", (now, device_id))
            conn.execute("UPDATE fleet_app_attempts SET revoked_at=%s "
                         "WHERE device_id=%s AND revoked_at IS NULL", (now, device_id))
            release_queued_for_device_in(conn, device_id, now=now)
            retire_device_fallback_references(conn, device_id)
            conn.execute("UPDATE players SET retired_at=%s,authority_epoch=authority_epoch+1 "
                         "WHERE id=%s", (now, player_id))
            self._audit(conn, "player_retired", player_id)

    def calibrate(self, frame_id: str, operation: str, expected_revision: int,
                  calibration: Calibration | None = None, *, expected_generation: int) -> dict:
        now = self.clock.utc()
        with self._equipment_write() as conn:
            from central.equipment_drain import require_unfenced_frame_in

            require_unfenced_frame_in(conn, frame_id)
            self._expire_previews(conn)
            frame = conn.execute("SELECT * FROM frames WHERE id=%s FOR UPDATE", (frame_id,)).fetchone()
            if not frame:
                raise RegistryError("unknown_frame", 404)
            current = Calibration.model_validate(frame["calibration"])
            if current.revision != expected_revision:
                raise RegistryError("calibration_revision_conflict")
            if frame["generation"] != expected_generation:
                raise RegistryError("binding_generation_conflict")
            if operation == "revert":
                conn.execute("UPDATE frames SET preview=NULL,preview_expires=NULL WHERE id=%s", (frame_id,))
                result = current.model_dump()
            elif operation in ("preview", "commit"):
                if not calibration:
                    raise RegistryError("calibration_required", 422)
                if not conn.execute("SELECT 1 FROM bindings WHERE frame_id=%s", (frame_id,)).fetchone():
                    raise RegistryError("frame_unbound")
                if operation == "preview":
                    proposed = calibration.model_copy(update={"revision": current.revision})
                    conn.execute("UPDATE frames SET preview=%s,preview_expires=%s WHERE id=%s",
                                 (Jsonb(proposed.model_dump()), now + 30, frame_id))
                    result = {"calibration": proposed.model_dump(), "expires_at": now + 30}
                else:
                    committed = calibration.model_copy(update={"revision": current.revision + 1})
                    conn.execute("UPDATE frames SET calibration=%s,calibration_valid=true,preview=NULL,"
                                 "preview_expires=NULL WHERE id=%s", (Jsonb(committed.model_dump()), frame_id))
                    result = committed.model_dump()
            else:
                raise RegistryError("invalid_calibration_operation", 422)
            conn.execute("UPDATE frames SET configuration_revision=configuration_revision+1 WHERE id=%s",
                         (frame_id,))
            self._audit(conn, f"calibration_{operation}", frame_id)
            return result

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
        self._expire_previews(conn)
        rows = conn.execute("SELECT f.*,b.output_id FROM bindings b JOIN frames f ON f.id=b.frame_id "
                            "WHERE b.player_id=%s ORDER BY b.output_id FOR SHARE OF f,b",
                            (player_id,)).fetchall()
        bindings = [OutputBinding(output_id=r["output_id"], frame_id=r["id"], generation=r["generation"],
                              configuration_revision=r["configuration_revision"],
                              profile=FrameProfile.model_validate(r["profile"]),
                              calibration=Calibration.model_validate(r["calibration"]),
                              preview=Calibration.model_validate(r["preview"]) if r["preview"] else None,
                              preview_expires=r["preview_expires"])
                for r in rows]
        return {"bindings": bindings, "execution_bindings": [binding for binding, row in
                zip(bindings, rows, strict=True) if row["calibration_valid"]]}

    def inventory(self) -> InstallationInventory:
        with self.db.transaction() as conn:
            now = self.clock.utc()
            self._expire_previews(conn)
            return self.inventory_in(conn, now)

    def inventory_in(self, conn, now: float) -> InstallationInventory:
        """Read inventory in a caller-owned transaction without writing expiry cleanup.

        Expired previews are omitted from the returned view. A later mutating registry
        operation may persist their expiry; this method is safe in read-only snapshots.
        """
        players = conn.execute("SELECT id,device_id,authority_epoch,registered_at,last_seen,retired_at,health "
                               "FROM players ORDER BY registered_at,id").fetchall()
        outputs = conn.execute("SELECT player_id,output_id,observation FROM outputs "
                               "ORDER BY player_id,output_id").fetchall()
        frames = conn.execute("SELECT f.id,f.surface_id,f.x_mm,f.y_mm,f.width_mm,f.height_mm,f.profile,"
                              "f.generation,f.calibration,f.calibration_valid,f.preview,f.preview_expires,"
                              "f.configuration_revision,b.player_id,b.output_id FROM frames f LEFT JOIN bindings b "
                              "ON b.frame_id=f.id ORDER BY f.id").fetchall()
        for frame in frames:
            if frame["preview_expires"] is not None and frame["preview_expires"] <= now:
                frame["preview"] = None
                frame["preview_expires"] = None
        bound_player_ids = {frame["player_id"] for frame in frames if frame["player_id"] is not None}
        return InstallationInventory(
            players=tuple(PlayerInventory.model_validate({**row, "is_bound": row["id"] in bound_player_ids})
                         for row in players),
            outputs=tuple(OutputInventory.model_validate(row) for row in outputs),
            frames=tuple(FrameInventory.model_validate(row) for row in frames),
        )
