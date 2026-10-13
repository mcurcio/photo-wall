"""The configuration snapshot's one read contract: the agent's and the console's (owner, 2026-10-13,
"View and restore"). One plain method per question; central/infra/tuning_history_store.py implements
it over PostgreSQL and central/tuning_history_routes.py depends only on it. The two Restores are
their owners' commands (central/displays/ports.py `PictureCommands.restore_picture`;
central/fleet/node_calibration.py `NodeCalibration.restore`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from central.displays.views import PictureEntryView, PictureHistoryView
from central.fleet.position_views import PositionHistoryView
from central.tuning_history.bounds import HistoryPage
from central.tuning_history.views import TuningSnapshotView


class TuningHistoryQueries(Protocol):
    def picture_history(self, display_id: UUID, page: HistoryPage) -> PictureHistoryView:
        """The Display's kept and sent picture entries, newest first by `at` then number.
        HistoryRefused("unknown_display", 404)."""
        ...

    def position_history(self, frame_id: str, page: HistoryPage) -> PositionHistoryView:
        """The Frame's Position entries, newest first by `at` then number, each with Restore's answer
        for it now. HistoryRefused("unknown_frame", 404)."""
        ...

    def snapshot_at(self, frame_id: str, at: datetime) -> TuningSnapshotView:
        """The Frame's snapshot at `at` (model.snapshot_at over the three latest-at reads).
        HistoryRefused("unknown_frame", 404)."""
        ...

    def sent_picture_entry(self, player_id: str, output_id: str, picture_change: int) -> PictureEntryView:
        """The sent picture entry of one port's picture change (picture_sent_entries' key; the
        calibration target's QR names it, decision 0026): what Central sent that port then, in the
        picture history's entry shape. HistoryRefused("unknown_entry", 404) when the port has no
        such entry (never sent, trimmed, an unknown player or port, or a change below 1)."""
        ...


__all__ = ["TuningHistoryQueries"]
