"""The configuration snapshot's shape (owner, 2026-10-13: "configuration snapshots"): one Frame at one
time, its Position entry and its Display's picture entry, each in its owner's shape
(central/fleet/position_views.py, central/displays/views.py). Plain JSON; times aware UTC.
"""
from __future__ import annotations

from pydantic import AwareDatetime

from central.displays.views import PictureEntryView
from central.fleet.position_views import PositionEntryView
from central.installation_models import FrameRefView
from contracts.models import Model


class TuningSnapshotView(Model):
    """GET /v1/operator/frames/{frame_id}/tuning-snapshot?at=...: what the Frame had at `at`
    (central/tuning_history/model.py `snapshot_at`). `position` None: no Position entry at or before
    `at`; `picture` None: no picture entry for it."""
    frame: FrameRefView
    at: AwareDatetime
    position: PositionEntryView | None
    picture: PictureEntryView | None


__all__ = ["TuningSnapshotView"]
