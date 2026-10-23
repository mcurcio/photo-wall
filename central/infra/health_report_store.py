"""Each Pi's newest health verdict, read from what Central already records (issue #126).

Central records every value it reads from a Node's bus (node_link_records, migration 066), the
`health` line's included; the record is the one source, so nothing here keeps a copy. The newest
record of the health line's HEALTH_REPORT_KEY per enrolled Player (by Central's `recorded_at`, then
sequence) is decoded; a KV operation (a removed key) or a value that does not decode reads as no
report for that Player (logged once per read).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from contracts.node_health import HEALTH_LINE, HEALTH_REPORT_KEY, HealthReport, decode_health_report
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream

log = logging.getLogger(__name__)

HEALTH_STATE_STREAM: Final = state_stream(HEALTH_LINE)
HEALTH_REPORT_SUBJECT: Final = state_prefix(HEALTH_LINE) + HEALTH_REPORT_KEY


def health_reports_in(conn: Any) -> Mapping[str, HealthReport]:
    """player id -> its Pi's newest decoded HealthReport, for every enrolled (not retired) Player
    whose device has one, in the caller's transaction (one query: DISTINCT ON device over
    node_link_records WHERE stream = HEALTH_STATE_STREAM AND subject = HEALTH_REPORT_SUBJECT,
    joined to players). Safe in read-only snapshots."""
    rows = conn.execute(
        "SELECT DISTINCT ON (r.device_id) p.id AS player_id, r.seq, r.data, r.headers"
        " FROM node_link_records r JOIN players p ON p.device_id = r.device_id AND p.retired_at IS NULL"
        " WHERE r.stream=%s AND r.subject=%s ORDER BY r.device_id, r.recorded_at DESC, r.seq DESC",
        (HEALTH_STATE_STREAM, HEALTH_REPORT_SUBJECT)).fetchall()
    reports: dict[str, HealthReport] = {}
    for row in rows:
        if (row["headers"] or {}).get(KV_OPERATION_HEADER):
            log.info("health verdict removed: player=%s seq=%s", row["player_id"], row["seq"])
            continue
        try:
            reports[row["player_id"]] = decode_health_report(bytes(row["data"]))
        except ValueError:
            log.warning("health verdict not decoded: player=%s seq=%s", row["player_id"], row["seq"])
    return reports


__all__ = ["HEALTH_REPORT_SUBJECT", "HEALTH_STATE_STREAM", "health_reports_in"]
