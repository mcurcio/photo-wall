"""What each Player drew with, from its own bus line (0017 E7/E8 pulled into #106; contracts/node_state.py):
a `RecordJudge` for the Player line's state bucket that reads each `room-lights-drawn` value
(contracts/room_light.py RoomLightsDrawn) as it is recorded and keeps it as that Player's latest
drawn report (`DrawnReports.record_drawn_within`), which House › Lights judges by age.

Each value is recorded in a savepoint of its own, so a failure rolls back only that report and is
logged: the Player line's record always commits. A value that does not decode is logged with
DrawnReportRefusal.INVALID and records nothing; a Node with no player records nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Final

from central.infra.transactions import PgTransaction
from central.room_light.model import DrawnReportRefusal
from central.room_light.ports import DrawnReports
from contracts.node_state import PLAYER_LINE, ROOM_LIGHTS_DRAWN_KEY
from contracts.room_light import ROOM_LIGHTS_DRAWN_BYTES, RoomLightsDrawn
from contracts.wire_model import decoded
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream
from nodeapi.pull import Read

log = logging.getLogger(__name__)

PLAYER_STATE_STREAM: Final = state_stream(PLAYER_LINE)
_SUBJECT: Final = state_prefix(PLAYER_LINE) + ROOM_LIGHTS_DRAWN_KEY


class DrawnReportJudge:
    """Records each newly recorded drawn report of a Pi's Player line as that Pi's Player's latest."""

    def __init__(self, reports: DrawnReports) -> None:
        self._reports = reports

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None:
        if stream != PLAYER_STATE_STREAM:
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
                report = decoded(RoomLightsDrawn, item.data, ROOM_LIGHTS_DRAWN_BYTES, "room_lights_drawn")
            except ValueError:
                log.warning("drawn report refused: %s device=%s seq=%s", DrawnReportRefusal.INVALID.value,
                            device_id, item.token.seq)
                continue
            try:
                self._reports.record_drawn_within(tx, player_id, report)
            except Exception:   # only this report rolled back; the Player line's record still commits
                log.exception("drawn report not recorded: device=%s seq=%s", device_id, item.token.seq)


__all__ = ["PLAYER_STATE_STREAM", "DrawnReportJudge"]
