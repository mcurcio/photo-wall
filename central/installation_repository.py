"""PostgreSQL adapter for Installation session queries."""

from __future__ import annotations

import hashlib

from contracts.models import Calibration, FrameProfile, OutputBinding
from contracts.time import Clock


class PostgresInstallationRepository:
    def __init__(self, clock: Clock):
        self.clock = clock

    @staticmethod
    def authenticate_in(conn, token: str) -> dict | None:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        return conn.execute(
            "SELECT id,authority_epoch FROM players WHERE token_hash=%s AND retired_at IS NULL",
            (digest,),
        ).fetchone()

    def active_sessions_in(self, conn) -> tuple[dict, ...]:
        return tuple(
            conn.execute(
                "SELECT id,authority_epoch FROM players WHERE retired_at IS NULL "
                "ORDER BY id FOR SHARE"
            ).fetchall()
        )

    @staticmethod
    def session_is_current_in(
        conn, player_id: str, authority_epoch: int, *, lock: bool = False
    ) -> bool:
        suffix = " FOR SHARE" if lock else ""
        return (
            conn.execute(
                "SELECT 1 FROM players WHERE id=%s AND authority_epoch=%s "
                "AND retired_at IS NULL" + suffix,
                (player_id, authority_epoch),
            ).fetchone()
            is not None
        )

    def frame_profiles_in(self, conn, frame_ids: set[str]) -> dict[str, FrameProfile]:
        if not frame_ids:
            return {}
        rows = conn.execute(
            "SELECT id,profile FROM frames WHERE id=ANY(%s)", (list(frame_ids),)
        ).fetchall()
        return {row["id"]: FrameProfile.model_validate(row["profile"]) for row in rows}

    def configuration_in(
        self, conn, player_id: str, authority_epoch: int
    ) -> dict[str, list[OutputBinding]] | None:
        if not self.session_is_current_in(conn, player_id, authority_epoch, lock=True):
            return None
        conn.execute(
            "UPDATE frames SET preview=NULL,preview_expires=NULL,"
            "configuration_revision=configuration_revision+1 WHERE preview_expires<=%s",
            (self.clock.utc(),),
        )
        rows = conn.execute(
            "SELECT f.*,b.output_id FROM bindings b JOIN frames f ON f.id=b.frame_id "
            "WHERE b.player_id=%s ORDER BY b.output_id FOR SHARE OF f,b",
            (player_id,),
        ).fetchall()
        bindings = [
            OutputBinding(
                output_id=row["output_id"],
                frame_id=row["id"],
                generation=row["generation"],
                configuration_revision=row["configuration_revision"],
                profile=FrameProfile.model_validate(row["profile"]),
                calibration=Calibration.model_validate(row["calibration"]),
                preview=(Calibration.model_validate(row["preview"]) if row["preview"] else None),
                preview_expires=row["preview_expires"],
            )
            for row in rows
        ]
        return {
            "bindings": bindings,
            "execution_bindings": [
                binding
                for binding, row in zip(bindings, rows, strict=True)
                if row["calibration_valid"]
            ],
        }
