"""The Pi light sensor's adapter: a `RecordJudge` for the host line's state bucket, which reads each
Pi's wired light sensor (contracts/room_light.py LIGHT_SENSOR_KEY) as it is recorded and hands it to
room lights as a reading of the reference (PI_SOURCE, the Pi's player id).

Each reading is recorded in a savepoint of its own (`RoomLightReports.record_reading_within`), so a
failure rolls back only that reading and is logged: the host line's record always commits. A value
that is not a reading is kept in the one refusal log.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Final

from central.infra.transactions import PgTransaction
from central.room_light.model import ReportRefusal, SensorRef
from central.room_light.ports import RoomLightReports
from contracts.models import IDENTIFIER_PATTERN
from contracts.room_light import LIGHT_SENSOR_KEY, decode_light_sensor
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream
from nodeapi.pull import Read

log = logging.getLogger(__name__)

PI_SOURCE: Final = "pi"                         # a Pi's sensor reference: (PI_SOURCE, its player id)
HOST_STATE_STREAM: Final = state_stream("host")
_SUBJECT: Final = state_prefix("host") + LIGHT_SENSOR_KEY
_PLAYER: Final = re.compile(IDENTIFIER_PATTERN)


def is_player(address: str) -> bool:
    """Whether `address` has a player id's form: the check a reference with PI_SOURCE takes before it
    is saved on a Sensor."""
    return _PLAYER.fullmatch(address) is not None


class LightSensorJudge:
    """Records each newly recorded light-sensor value of a Pi's host line as that Pi's reading; a
    Node with no player records nothing."""

    def __init__(self, reports: RoomLightReports) -> None:
        self._reports = reports

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None:
        if stream != HOST_STATE_STREAM:
            return
        values = [item for item in items
                  if item.subject == _SUBJECT and not (item.headers or {}).get(KV_OPERATION_HEADER)]
        if not values:
            return
        row = conn.execute("SELECT id FROM players WHERE device_id=%s AND retired_at IS NULL",
                           (device_id,)).fetchone()
        if row is None:
            return
        player_id = row["id"]
        for item in values:
            try:
                reading = decode_light_sensor(item.data)
            except ValueError:
                self._reports.record_refused(PI_SOURCE, player_id, ReportRefusal.UNREADABLE)
                continue
            try:
                self._reports.record_reading_within(PgTransaction(conn), SensorRef(source=PI_SOURCE, address=player_id),
                                                    reading.reading)
            except Exception:   # only this reading rolled back; the host line's record still commits
                log.exception("light sensor reading not recorded: device=%s seq=%s", device_id, item.token.seq)


__all__ = ["HOST_STATE_STREAM", "PI_SOURCE", "LightSensorJudge", "is_player"]
