"""Frame lighting over PostgreSQL: `PgFrameLighting` implements central/frame_lighting/ports.py's
FrameLightingCheck, and this module holds the house's one lock and the one "which Frames name it"
query.

The house's lock is the `room_lights_change` row: every Frame-lighting check takes it shared
(`share_house_lock`), every writer and remover of a room light, a Sensor or a paper profile takes it
exclusive (`take_house_lock`), first. So no removal commits between a check and its write, and two
checks never wait for each other.
"""
from __future__ import annotations

from typing import Any

from central.frame_lighting.model import FrameLightingRefused, FramesNaming, LightingHouse
from central.frame_lighting.policy import check, naming, new_frame_lighting, stamped
from central.infra.room_light_rows import lights_in
from central.infra.transactions import pg_connection
from central.kernel.transactions import Transaction
from contracts.frame_lighting import FrameLighting
from contracts.node_picture import DisplayLight

_LIGHTINGS = ("SELECT id AS frame_id, lighting FROM frames UNION ALL "
              "SELECT frame_id, lighting FROM node_calibration_trials WHERE state='active'")


def share_house_lock(conn: Any) -> None:
    """Take the house's lock shared, for the caller's transaction."""
    conn.execute("SELECT change FROM room_lights_change WHERE one FOR SHARE")


def take_house_lock(conn: Any) -> None:
    """Take the house's lock exclusive, for the caller's transaction."""
    conn.execute("SELECT change FROM room_lights_change WHERE one FOR UPDATE")


def naming_in(conn: Any) -> FramesNaming:
    """Which Frames' saved lighting or open Position trial names each room light and paper profile."""
    return naming((row["frame_id"], FrameLighting.model_validate(row["lighting"]))
                  for row in conn.execute(_LIGHTINGS).fetchall())


def _house_in(conn: Any) -> LightingHouse:
    profiles = conn.execute("SELECT profile_sha256 FROM luts").fetchall()
    return LightingHouse(lights=lights_in(conn), paper_profiles=frozenset(row["profile_sha256"] for row in profiles))


class PgFrameLighting:
    """FrameLightingCheck over the caller's transaction; `recipe_id` is the recipe Central bakes paper
    profiles with today."""

    def __init__(self, recipe_id: str) -> None:
        self._recipe_id = recipe_id

    def fitted_within(self, tx: Transaction, lighting: FrameLighting) -> FrameLighting:
        conn = pg_connection(tx)
        share_house_lock(conn)
        refusal = check(lighting, _house_in(conn))
        if refusal is not None:
            raise FrameLightingRefused(refusal)
        return stamped(lighting, self._recipe_id)

    def new_frame_within(self, tx: Transaction, display: DisplayLight) -> FrameLighting:
        conn = pg_connection(tx)
        share_house_lock(conn)
        return new_frame_lighting(display, _house_in(conn))

    def naming_within(self, tx: Transaction) -> FramesNaming:
        conn = pg_connection(tx)
        share_house_lock(conn)
        return naming_in(conn)


__all__ = ["PgFrameLighting", "naming_in", "share_house_lock", "take_house_lock"]
