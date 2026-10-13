"""The configuration snapshot's reads over PostgreSQL (`TuningHistoryQueries`; migration 096). Each
method runs in one transaction and reads through its owner's store (picture_history_store,
position_history_store); names (the Display's, the Frame's, a LUT's) are read today. It never
writes.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from central.db import Database
from central.displays.views import PictureHistoryView
from central.fleet.position_views import PositionHistoryView
from central.tuning_history.model import HistoryPage
from central.tuning_history.views import TuningSnapshotView


class PgTuningHistory:
    """`TuningHistoryQueries` over PostgreSQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def picture_history(self, display_id: UUID, page: HistoryPage) -> PictureHistoryView:
        """HistoryRefused("unknown_display", 404). `unrecorded_change` is
        `picture_history.unrecorded_record(latest kept, stored) is not None`."""
        ...

    def position_history(self, frame_id: str, page: HistoryPage) -> PositionHistoryView:
        """HistoryRefused("unknown_frame", 404). Each entry's `restore_refusal` is
        `position_history.restore_refusal` against the Frame's profile and the Display seen on its
        port now; `unrecorded_change` is `position_history.unrecorded_record(latest kept, stored) is
        not None`."""
        ...

    def snapshot_at(self, frame_id: str, at: datetime) -> TuningSnapshotView:
        """HistoryRefused("unknown_frame", 404). `model.snapshot_at` over
        `latest_position_at_in`, `latest_sent_for_frame_at_in` and, for the Display seen on the
        Frame's port now (none: no kept read), `latest_kept_at_in`."""
        ...


__all__ = ["PgTuningHistory"]
