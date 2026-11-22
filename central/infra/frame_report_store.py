"""Each Player's newest Frame reports, read from what Central already records (decision 0036).

Central records every value it reads from a Node's bus (node_states, migration 119), the Player
line's included, the newest value per state key; the record is the one source, so nothing here
keeps a copy. The newest value of the Player line's FRAMES_KEY per enrolled Player is decoded; a
removed key reads as no Frame drawn, and a value that does not decode as no report for that Player
(logged once per read).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from central.infra.node_link_latest import latest_values_in
from contracts.node_state import FRAMES_KEY, PLAYER_LINE, FrameReports, decode_frame_reports


@dataclass(frozen=True, slots=True)
class RecordedFrameReports:
    """A Player's newest FrameReports and when Central recorded it (the database's clock)."""
    recorded_at: float
    reports: FrameReports


def frame_reports_in(conn: Any) -> Mapping[str, RecordedFrameReports]:
    """player id -> its newest decoded FrameReports with its record time, for every enrolled (not
    retired) Player whose device has one, in the caller's transaction (node_link_latest.py). Safe
    in read-only snapshots."""
    found: dict[str, RecordedFrameReports] = {}
    for latest in latest_values_in(conn, PLAYER_LINE, FRAMES_KEY):
        reports = (FrameReports(frames=()) if latest.removed
                   else latest.decoded(decode_frame_reports, "frame reports"))
        if reports is not None and latest.player_id is not None:
            found[latest.player_id] = RecordedFrameReports(latest.recorded_at, reports)
    return found


__all__ = ["RecordedFrameReports", "frame_reports_in"]
