"""The Pi light sensor's adapter: a `RecordJudge` for the host line's state bucket, which reads each
Pi's wired light sensor (contracts/room_light.py LIGHT_SENSOR_KEY) as it is recorded and hands it to
room lights as a reading of the reference (PI_SOURCE, the Pi's player id).

Each reading, and each value that is not a reading (kept in the one refusal log), is recorded in a
savepoint of its own (`RoomLightReports.record_reading_within`, `record_refused_within`), so a
failure rolls back only that word and is logged: the host line's record always commits. The record
stays the source: `missed_readings_in` names each Pi whose newest recorded reading its word has not
taken, and the room-lights document takes them before it is built.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Final

from central.central_errors.ports import Reference
from central.infra.node_link_latest import latest_values_in
from central.infra.transactions import PgTransaction
from central.room_light.model import DoubtReason, ReportRefusal, SensorRef
from central.room_light.ports import RoomLightReports
from contracts.models import IDENTIFIER_PATTERN
from contracts.room_light import (
    LIGHT_SENSOR_KEY,
    Reading,
    decode_light_sensor,
)
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream
from nodeapi.pull import Read

log = logging.getLogger(__name__)

PI_SOURCE: Final = "pi"                         # a Pi's sensor reference: (PI_SOURCE, its player id)
HOST_LINE: Final = "host"
HOST_STATE_STREAM: Final = state_stream(HOST_LINE)
_SUBJECT: Final = state_prefix(HOST_LINE) + LIGHT_SENSOR_KEY
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
        tx = PgTransaction(conn)
        for item in values:
            try:
                try:
                    reading = decode_light_sensor(item.data)
                except ValueError:
                    self._reports.record_refused_within(tx, PI_SOURCE, player_id, ReportRefusal.UNREADABLE)
                    continue
                self._reports.record_reading_within(tx, SensorRef(source=PI_SOURCE, address=player_id),
                                                    reading.reading)
            except Exception:   # only this word rolled back; the host line's record still commits
                log.exception("light sensor value not recorded: device=%s seq=%s", device_id, item.token.seq)


def missed_readings_in(conn: Any) -> tuple[tuple[str, Reading], ...]:
    """(player id, reading) for each enrolled Pi whose newest recorded light-sensor value is a reading
    its word (reading_words, PI_SOURCE) was not taken from: heard before that record, or never. A
    value that is not a reading, or a removed key, takes nothing (the judge logged the refusal)."""
    heard = {row["address"]: float(row["heard"]) for row in conn.execute(
        "SELECT address, EXTRACT(EPOCH FROM heard_at) AS heard FROM reading_words WHERE source=%s",
        (PI_SOURCE,)).fetchall()}
    missed: list[tuple[str, Reading]] = []
    for latest in latest_values_in(conn, HOST_LINE, LIGHT_SENSOR_KEY):
        if heard.get(latest.player_id, float("-inf")) >= latest.recorded_at or latest.removed:
            continue
        try:
            missed.append((latest.player_id, decode_light_sensor(latest.data).reading))
        except ValueError:
            continue
    return tuple(missed)


class PiMeterAdvice:
    """SourceAdvice (central/central_errors/ports.py) for PI_SOURCE references (the address is the
    Pi's player id): its reading can't be read -> install the current release on it; any other doubt
    (it can't tell; a Pi's meter has no link of its own to close) -> check its light meter."""

    def what_to_do(self, reference: Reference, reason: DoubtReason) -> str:
        if reason == DoubtReason.CANT_READ:
            return f"Install the current release on Player {reference.address}"
        return f"Check the light meter on Player {reference.address}"


__all__ = ["HOST_STATE_STREAM", "PI_SOURCE", "LightSensorJudge", "PiMeterAdvice", "is_player", "missed_readings_in"]
