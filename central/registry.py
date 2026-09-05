"""Registry authority: observations cannot mutate desired Frame configuration."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Annotated

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from central.db import Database
from contracts.models import Calibration, FrameProfile, Identifier, Model, OutputBinding
from contracts.time import Clock


class RegistryError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class OutputReport(Model):
    output_id: Identifier
    width_px: int = Field(ge=0, le=16384)
    height_px: int = Field(ge=0, le=16384)
    connected: bool = True


class Enrollment(Model):
    public_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    nonce: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    signature: Annotated[str, Field(min_length=88, max_length=88)]
    outputs: tuple[OutputReport, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def unique_outputs(self):
        if len({o.output_id for o in self.outputs}) != len(self.outputs):
            raise ValueError("duplicate output")
        return self


def enrollment_message(nonce: str, outputs: tuple[OutputReport, ...]) -> bytes:
    return json.dumps({"purpose": "photo-wall-enroll-v1", "nonce": nonce,
                       "outputs": [o.model_dump() for o in outputs]},
                      sort_keys=True, separators=(",", ":")).encode()


class FrameCreate(Model):
    id: Identifier
    surface_id: Identifier = "wall"
    x_mm: float = 0
    y_mm: float = 0
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    profile: FrameProfile

    @model_validator(mode="after")
    def oriented_profile(self):
        if self.height_mm != self.width_mm and ((self.height_mm > self.width_mm) !=
                                                (self.profile.height_px > self.profile.width_px)):
            raise ValueError("display profile must use dimensions oriented to the physical Frame")
        return self


class Registry:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

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
            retired = conn.execute("SELECT retired_at FROM players WHERE public_key=%s",
                                   (public_key,)).fetchone()
            if retired and retired["retired_at"] is not None:
                raise RegistryError("retired_player", 403)
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
                signature, enrollment_message(request.nonce, request.outputs)
            )
        except (ValueError, InvalidSignature) as exc:
            raise RegistryError("invalid_proof", 403) from exc
        now = self.clock.utc()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        player_id = "p-" + hashlib.sha256(bytes.fromhex(request.public_key)).hexdigest()[:32]
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734118322)")
            challenge = conn.execute("DELETE FROM enrollment_nonces WHERE nonce=%s "
                                     "AND public_key=%s AND expires_at>%s RETURNING nonce",
                                     (request.nonce, request.public_key, now)).fetchone()
            if not challenge:
                raise RegistryError("expired_or_used_challenge", 403)
            old = conn.execute("SELECT * FROM players WHERE id=%s FOR UPDATE", (player_id,)).fetchone()
            if old and old["retired_at"] is not None:
                raise RegistryError("retired_player", 403)
            if old:
                conn.execute("UPDATE players SET token_hash=%s,last_seen=%s,"
                             "authority_epoch=authority_epoch+1 WHERE id=%s",
                             (token_hash, now, player_id))
            else:
                conn.execute("INSERT INTO players(id,public_key,token_hash,registered_at,last_seen) "
                             "VALUES(%s,%s,%s,%s,%s)",
                             (player_id, request.public_key, token_hash, now, now))
            conn.execute("UPDATE outputs SET observation=jsonb_set(observation,'{connected}','false') "
                         "WHERE player_id=%s", (player_id,))
            for output in request.outputs:
                conn.execute("INSERT INTO outputs VALUES(%s,%s,%s) ON CONFLICT(player_id,output_id) "
                             "DO UPDATE SET observation=excluded.observation",
                             (player_id, output.output_id, Jsonb(output.model_dump())))
            epoch = conn.execute("SELECT authority_epoch FROM players WHERE id=%s",
                                 (player_id,)).fetchone()["authority_epoch"]
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

    def bind(self, frame_id: str, player_id: str, output_id: str, *, expected_generation: int) -> dict:
        try:
            with self.db.transaction() as conn:
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

    def retire(self, player_id: str) -> None:
        with self.db.transaction() as conn:
            player = conn.execute("SELECT * FROM players WHERE id=%s FOR UPDATE", (player_id,)).fetchone()
            if not player:
                raise RegistryError("unknown_player", 404)
            if player["retired_at"] is not None:
                return
            conn.execute("UPDATE players SET retired_at=%s,authority_epoch=authority_epoch+1 "
                         "WHERE id=%s", (self.clock.utc(), player_id))
            conn.execute("UPDATE frames SET generation=generation+1,calibration_valid=false,"
                         "configuration_revision=configuration_revision+1,"
                         "preview=NULL,preview_expires=NULL WHERE id IN "
                         "(SELECT frame_id FROM bindings WHERE player_id=%s)", (player_id,))
            conn.execute("DELETE FROM bindings WHERE player_id=%s", (player_id,))
            self._audit(conn, "player_retired", player_id)

    def calibrate(self, frame_id: str, operation: str, expected_revision: int,
                  calibration: Calibration | None = None, *, expected_generation: int) -> dict:
        now = self.clock.utc()
        with self.db.transaction() as conn:
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
            if not conn.execute("SELECT 1 FROM players WHERE id=%s AND authority_epoch=%s "
                                "AND retired_at IS NULL FOR SHARE", (player_id, epoch)).fetchone():
                raise RegistryError("stale_authority", 403)
            self._expire_previews(conn)
            rows = conn.execute("SELECT f.*,b.output_id FROM bindings b JOIN frames f ON f.id=b.frame_id "
                                "WHERE b.player_id=%s ORDER BY b.output_id", (player_id,)).fetchall()
            bindings = [OutputBinding(output_id=r["output_id"], frame_id=r["id"], generation=r["generation"],
                                  configuration_revision=r["configuration_revision"],
                                  profile=FrameProfile.model_validate(r["profile"]),
                                  calibration=Calibration.model_validate(r["calibration"]),
                                  preview=Calibration.model_validate(r["preview"]) if r["preview"] else None,
                                  preview_expires=r["preview_expires"])
                    for r in rows]
            return {"bindings": bindings, "execution_bindings": [binding for binding, row in
                    zip(bindings, rows, strict=True) if row["calibration_valid"]]}

    def inventory(self) -> dict:
        with self.db.transaction() as conn:
            self._expire_previews(conn)
            players = conn.execute("SELECT id,authority_epoch,registered_at,last_seen,retired_at,health "
                                   "FROM players ORDER BY registered_at,id").fetchall()
            outputs = conn.execute("SELECT * FROM outputs ORDER BY player_id,output_id").fetchall()
            frames = conn.execute("SELECT f.*,b.player_id,b.output_id FROM frames f LEFT JOIN bindings b "
                                  "ON b.frame_id=f.id ORDER BY f.id").fetchall()
            for frame in frames:
                if frame["preview_expires"] is not None and frame["preview_expires"] <= self.clock.utc():
                    frame["preview"] = None
                    frame["preview_expires"] = None
            return {"players": players, "outputs": outputs, "frames": frames}
