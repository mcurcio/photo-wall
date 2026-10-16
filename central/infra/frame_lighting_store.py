"""Frame lighting over PostgreSQL: central/frame_lighting/ports.py's two ports, `PgFrameLighting`
(FrameLightingCheck) and `PgHouseLock` (HouseLock: the one home of the house's lock and of the "which
Frames name it" query; every store that writes the house, central/infra/room_light_store.py and
paper_profile_store.py, is handed one and reaches both only through it).

The house's lock is the `room_lights_change` row: every Frame-lighting check takes it shared, every
writer and remover of a room light, a Sensor, a reporter's word or a paper profile takes it exclusive
(`changing_within`), first. So no removal commits between a check and its write, and two checks never
wait for each other.
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


def _share_lock(conn: Any) -> None:
    conn.execute("SELECT change FROM room_lights_change WHERE one FOR SHARE")


def _house_in(conn: Any) -> LightingHouse:
    profiles = conn.execute("SELECT profile_sha256 FROM paper_profiles").fetchall()
    return LightingHouse(lights=lights_in(conn), paper_profiles=frozenset(row["profile_sha256"] for row in profiles))


class PgFrameLighting:
    """FrameLightingCheck over the caller's transaction; `recipe_id` is the recipe Central bakes paper
    profiles with today."""

    def __init__(self, recipe_id: str) -> None:
        self._recipe_id = recipe_id

    def fitted_within(self, tx: Transaction, lighting: FrameLighting) -> FrameLighting:
        conn = pg_connection(tx)
        _share_lock(conn)
        refusal = check(lighting, _house_in(conn))
        if refusal is not None:
            raise FrameLightingRefused(refusal)
        return stamped(lighting, self._recipe_id)

    def new_frame_within(self, tx: Transaction, display: DisplayLight) -> FrameLighting:
        conn = pg_connection(tx)
        _share_lock(conn)
        return new_frame_lighting(display, _house_in(conn))


class PgHouseLock:
    """HouseLock over the caller's transaction."""

    def changing_within(self, tx: Transaction) -> None:
        pg_connection(tx).execute("SELECT change FROM room_lights_change WHERE one FOR UPDATE")

    def naming_within(self, tx: Transaction) -> FramesNaming:
        conn = pg_connection(tx)
        _share_lock(conn)
        return naming((row["frame_id"], FrameLighting.model_validate(row["lighting"]))
                      for row in conn.execute(_LIGHTINGS).fetchall())


__all__ = ["PgFrameLighting", "PgHouseLock"]
