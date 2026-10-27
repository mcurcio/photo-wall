"""Central's errors over PostgreSQL: `PgCentralErrors` implements central/central_errors/ports.py
CentralErrors from what Central already keeps, never a copy. What is in doubt is room lights'
(central/infra/room_light_store.py), which Frames name each light the house's
(central/frame_lighting/ports.py HouseLock), what to do each source's adapter's (`SourceAdvice`,
composed by central/app.py), and what a Frame's Player said is the newest record of its Player
line's IMPEDED_KEY (contracts/node_impeded.py) in node_link_records, as health_report_store.py
reads the health line's.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from central.central_errors.compose import impediment, in_doubt
from central.central_errors.ports import SourceAdvice
from central.central_errors.views import CentralErrorsView, FrameImpedimentView, FrameNotReported
from central.db import Database
from central.frame_lighting.ports import HouseLock
from central.infra.room_light_rows import lights_in
from central.infra.room_light_store import colour_sensors_in, facts_in, sensors_in
from central.infra.transactions import PgTransaction
from central.room_light.model import Source
from contracts.identifiers import TargetIdentifier
from contracts.node_impeded import IMPEDED_KEY, ImpededFrames, decode_impeded
from contracts.node_state import PLAYER_LINE
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream

log = logging.getLogger(__name__)

PLAYER_STATE_STREAM: Final = state_stream(PLAYER_LINE)
IMPEDED_SUBJECT: Final = state_prefix(PLAYER_LINE) + IMPEDED_KEY


class PgCentralErrors:
    """CentralErrors over one Database."""

    def __init__(self, db: Database, house: HouseLock, advice: Mapping[Source, SourceAdvice]) -> None:
        """`advice`: each reporting source's SourceAdvice, by its source name."""
        self._db = db
        self._house = house
        self._advice = dict(advice)

    def in_doubt(self) -> CentralErrorsView:
        with self._db.transaction() as conn:
            naming = self._house.naming_within(PgTransaction(conn)).lights
            lights, sensors, colour_sensors, facts = lights_in(conn), sensors_in(conn), colour_sensors_in(conn), facts_in(conn)
        return in_doubt(lights, sensors, colour_sensors, facts, naming, self._advice)

    def impeded(self, frame_id: TargetIdentifier) -> FrameImpedimentView:
        with self._db.read_snapshot() as conn:
            binding = conn.execute(
                "SELECT b.output_id, p.device_id FROM bindings b JOIN players p ON p.id = b.player_id "
                "AND p.retired_at IS NULL WHERE b.frame_id=%s", (frame_id,)).fetchone()
            if binding is None or binding["device_id"] is None:
                return FrameImpedimentView(frame_id=frame_id, impediment=FrameNotReported())
            report = _impeded_in(conn, binding["device_id"])
        return FrameImpedimentView(frame_id=frame_id, impediment=impediment(frame_id, binding["output_id"], report))


def _impeded_in(conn: Any, device_id: str) -> ImpededFrames | None:
    """The newest record of the device's Player line IMPEDED_KEY (by Central's `recorded_at`, then
    sequence): None when there is none, or one that does not decode (logged); a removed key reads as
    no Frame impeded."""
    row = conn.execute(
        "SELECT seq, data, headers FROM node_link_records WHERE device_id=%s AND stream=%s AND subject=%s "
        "ORDER BY recorded_at DESC, seq DESC LIMIT 1", (device_id, PLAYER_STATE_STREAM, IMPEDED_SUBJECT)).fetchone()
    if row is None:
        return None
    if (row["headers"] or {}).get(KV_OPERATION_HEADER):
        return ImpededFrames(frames=())
    try:
        return decode_impeded(bytes(row["data"]))
    except ValueError:
        log.warning("impeded report not decoded: device=%s seq=%s", device_id, row["seq"])
        return None


__all__ = ["IMPEDED_SUBJECT", "PLAYER_STATE_STREAM", "PgCentralErrors"]
