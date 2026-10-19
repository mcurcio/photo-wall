"""PostgreSQL adapter for Installation session queries."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from central.displays.model import Readiness
from central.infra.display_store import (
    DISPLAY_SIZE_COLUMNS,
    DISPLAY_SIZE_JOIN,
    POSITION_COLUMNS,
    SEEN_DISPLAY_JOIN,
    frame_display_mm,
    frame_readiness,
)
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration, OutputBinding
from contracts.time import Clock


def output_binding(row: Mapping[str, Any]) -> OutputBinding:
    """The one mapping of a frames row joined with its binding (`output_id`) and its seen Display's
    size (DISPLAY_SIZE_COLUMNS) to an OutputBinding (frame box run fb-9): the Frame's generation,
    configuration revision, Position and lighting, and the Display's picture area as built
    (`frame_display_mm`; None when none is seen or it states none)."""
    return OutputBinding(
        output_id=row["output_id"],
        frame_id=row["id"],
        generation=row["generation"],
        configuration_revision=row["configuration_revision"],
        calibration=Calibration.model_validate(row["calibration"]),
        lighting=FrameLighting.model_validate(row["lighting"]),
        display_mm=frame_display_mm(row),
    )


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

    @staticmethod
    def bound_player_count_in(conn) -> int:
        """Number of players adopted into a frame (owning a `bindings` row).

        A `bindings` row is the sole "bound" signal: a player that netboots or
        enrolls but is never adopted into a frame has no row and is excluded, and
        `Registry.retire` deletes a retired player's bindings, so retired players
        are excluded too. This is the same table `configuration_in`/`inventory`
        derive `is_bound` from -- the count is not re-deriving that logic, it
        aggregates the same rows.
        """
        return conn.execute(
            "SELECT count(DISTINCT player_id) AS n FROM bindings"
        ).fetchone()["n"]

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

    def configuration_in(
        self, conn, player_id: str, authority_epoch: int
    ) -> dict[str, list[OutputBinding]] | None:
        """The Player's bindings (`output_binding` of each), and those whose Frame is ready, under
        a share lock on its session, Frames and bindings; None when the session is not current.
        The one query of a Player's configuration (Registry.configuration_in delegates here)."""
        if not self.session_is_current_in(conn, player_id, authority_epoch, lock=True):
            return None
        rows = conn.execute(
            f"SELECT f.*,b.output_id,{POSITION_COLUMNS},{DISPLAY_SIZE_COLUMNS} FROM bindings b "  # noqa: S608
            f"JOIN frames f ON f.id=b.frame_id {SEEN_DISPLAY_JOIN} {DISPLAY_SIZE_JOIN} "
            "WHERE b.player_id=%s ORDER BY b.output_id FOR SHARE OF f,b",
            (player_id,),
        ).fetchall()
        bindings = [output_binding(row) for row in rows]
        return {
            "bindings": bindings,
            "execution_bindings": [
                binding
                for binding, row in zip(bindings, rows, strict=True)
                if frame_readiness(row) is Readiness.READY
            ],
        }
