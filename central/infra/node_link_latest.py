"""The newest value a Node's line stated under one state key, read from what Central already records
(node_states, migration 119: one row per key, the newest the link store stored): the one home of
"newest value of (line stream, key subject) per enrolled Player's device; a KV operation reads as
removed; decode it or log it". Its readers: each Pi's health verdict (health_report_store.py),
a Pi's light reading its word missed (light_sensor_judge.py) and what a Frame's Player said about
Central's error (central_errors_store.py) and the base each Pi's host states, which ends its Install
(player_versions.py: every enrolled Pi's device, a Player or not). Safe in read-only snapshots.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nodeapi.buffers import state_prefix, state_stream

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LatestValue:
    """One device's newest record of a line's state key."""
    player_id: str | None   # None only when read for every enrolled device (`of_players=False`)
    device_id: str
    seq: int
    recorded_at: float   # Central's clock, epoch seconds
    removed: bool        # a KV operation: the Node removed the key
    data: bytes

    def decoded[T](self, decode: Callable[[bytes], T], what: str) -> T | None:
        """The value decoded by `decode`; None when removed, or when it does not decode (logged as
        `what`)."""
        if self.removed:
            return None
        try:
            return decode(self.data)
        except ValueError:
            log.warning("%s not decoded: player=%s seq=%s", what, self.player_id, self.seq)
            return None


def latest_values_in(conn: Any, line: str, key: str, *, device_id: str | None = None,
                     of_players: bool = True) -> tuple[LatestValue, ...]:
    """The newest record of `line`'s state `key` for every enrolled (not retired) Player's device, or
    only `device_id`'s, in the caller's transaction (node_states holds one row per key). With
    `of_players=False`, for every enrolled (not retired) device, its Player's id when it has one."""
    enrolled = ("JOIN players p ON p.device_id = s.device_id AND p.retired_at IS NULL" if of_players else
                "JOIN devices d ON d.device_id = s.device_id AND d.retired_at IS NULL"
                " LEFT JOIN players p ON p.device_id = s.device_id AND p.retired_at IS NULL")
    rows = conn.execute(
        "SELECT p.id AS player_id, s.device_id, s.seq, s.recorded_at, s.data, s.removed"
        f" FROM node_states s {enrolled}"
        " WHERE s.stream=%(stream)s AND s.subject=%(subject)s"
        " AND (%(device)s::text IS NULL OR s.device_id = %(device)s)"
        " ORDER BY s.device_id",
        {"stream": state_stream(line), "subject": state_prefix(line) + key, "device": device_id}).fetchall()
    return tuple(LatestValue(player_id=row["player_id"], device_id=row["device_id"], seq=int(row["seq"]),
                             recorded_at=float(row["recorded_at"]),
                             removed=bool(row["removed"]),
                             data=bytes(row["data"] or b"")) for row in rows)


__all__ = ["LatestValue", "latest_values_in"]
