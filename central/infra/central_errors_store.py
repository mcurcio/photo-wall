"""Central's errors over PostgreSQL: `PgCentralErrors` implements central/central_errors/ports.py
CentralErrors from what Central already keeps, never a copy. What is in doubt is room lights'
(central/infra/room_light_store.py), which Frames name each light the house's
(central/frame_lighting/ports.py HouseLock), what to do each source's adapter's (`SourceAdvice`,
composed by central/app.py), where a Frame is shown the Registry's (`FrameOutputs`), and what a
Frame's Player said is the newest record of its Player line's IMPEDED_KEY
(contracts/node_impeded.py) in node_link_records (central/infra/node_link_latest.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from central.central_errors.compose import impediment, in_doubt
from central.central_errors.ports import FrameOutputs, SourceAdvice
from central.central_errors.views import CentralErrorsView, FrameImpedimentView, FrameNotReported
from central.db import Database
from central.frame_lighting.ports import HouseLock
from central.infra.node_link_latest import latest_values_in
from central.infra.room_light_rows import lights_in
from central.infra.room_light_store import colour_sensors_in, facts_in, sensors_in
from central.infra.transactions import PgTransaction
from central.room_light.model import Source
from contracts.identifiers import TargetIdentifier
from contracts.node_impeded import IMPEDED_KEY, ImpededFrames, decode_impeded
from contracts.node_state import PLAYER_LINE
from nodeapi.buffers import state_prefix, state_stream

PLAYER_STATE_STREAM: Final = state_stream(PLAYER_LINE)
IMPEDED_SUBJECT: Final = state_prefix(PLAYER_LINE) + IMPEDED_KEY


class PgCentralErrors:
    """CentralErrors over one Database."""

    def __init__(self, db: Database, house: HouseLock, advice: Mapping[Source, SourceAdvice],
                 outputs: FrameOutputs) -> None:
        """`advice`: each reporting source's SourceAdvice, by its source name; `outputs`: where each
        Frame is shown now."""
        self._db = db
        self._house = house
        self._advice = dict(advice)
        self._outputs = outputs

    def in_doubt(self) -> CentralErrorsView:
        with self._db.transaction() as conn:
            naming = self._house.naming_within(PgTransaction(conn)).lights
            lights, sensors, colour_sensors, facts = lights_in(conn), sensors_in(conn), colour_sensors_in(conn), facts_in(conn)
        return in_doubt(lights, sensors, colour_sensors, facts, naming, self._advice)

    def impeded(self, frame_id: TargetIdentifier) -> FrameImpedimentView:
        with self._db.read_snapshot() as conn:
            bound = self._outputs.bound_output_within(PgTransaction(conn), frame_id)
            if bound is None:
                return FrameImpedimentView(frame_id=frame_id, impediment=FrameNotReported())
            report = _impeded_in(conn, bound.device_id)
        return FrameImpedimentView(frame_id=frame_id, impediment=impediment(frame_id, bound.output_id, report))


def _impeded_in(conn: Any, device_id: str) -> ImpededFrames | None:
    """The newest record of the device's Player line IMPEDED_KEY (node_link_latest.py): None when
    there is none, or one that does not decode (logged); a removed key reads as no Frame impeded."""
    for latest in latest_values_in(conn, PLAYER_LINE, IMPEDED_KEY, device_id=device_id):
        return ImpededFrames(frames=()) if latest.removed else latest.decoded(decode_impeded, "impeded report")
    return None


__all__ = ["IMPEDED_SUBJECT", "PLAYER_STATE_STREAM", "PgCentralErrors"]
