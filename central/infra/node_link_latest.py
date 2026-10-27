"""The newest value a Node's line stated under one state key, read from what Central already records
(node_link_records, migration 066): the one home of "newest record of (line stream, key subject)
per enrolled Player's device, by Central's `recorded_at`, then sequence; a KV operation reads as
removed; decode it or log it". Its readers: each Pi's health verdict (health_report_store.py),
a Pi's light reading its word missed (light_sensor_judge.py) and what a Frame's Player said about
Central's error (central_errors_store.py). Safe in read-only snapshots.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LatestValue:
    """One device's newest record of a line's state key."""
    player_id: str
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


def latest_values_in(conn: Any, line: str, key: str, *, device_id: str | None = None) -> tuple[LatestValue, ...]:
    """The newest record of `line`'s state `key` for every enrolled (not retired) Player's device, or
    only `device_id`'s, in the caller's transaction (one DISTINCT ON query)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (r.device_id) p.id AS player_id, r.device_id, r.seq, r.recorded_at, r.data, r.headers"
        " FROM node_link_records r JOIN players p ON p.device_id = r.device_id AND p.retired_at IS NULL"
        " WHERE r.stream=%(stream)s AND r.subject=%(subject)s"
        " AND (%(device)s::text IS NULL OR r.device_id = %(device)s)"
        " ORDER BY r.device_id, r.recorded_at DESC, r.seq DESC",
        {"stream": state_stream(line), "subject": state_prefix(line) + key, "device": device_id}).fetchall()
    return tuple(LatestValue(player_id=row["player_id"], device_id=row["device_id"], seq=int(row["seq"]),
                             recorded_at=float(row["recorded_at"]),
                             removed=bool((row["headers"] or {}).get(KV_OPERATION_HEADER)),
                             data=bytes(row["data"] or b"")) for row in rows)


__all__ = ["LatestValue", "latest_values_in"]
