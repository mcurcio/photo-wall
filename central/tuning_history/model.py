"""The configuration snapshot's rule, pure: the one rule that composes a Frame's snapshot at a minute
from its owners' histories. Retention, the page and Central's time are central/tuning_history/bounds.py's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from central.displays.picture_history import KEPT_KINDS, SENT_KINDS, PictureEntry
from central.fleet.position_history import PositionEntry


@dataclass(frozen=True)
class TuningSnapshot:
    """One Frame at one time (the owner's "configuration snapshot"): its newest Position entry at or
    before `at`, and the picture entry that says what its Display had then (`snapshot_at`)."""
    frame_id: str
    at: datetime
    position: PositionEntry | None
    picture: PictureEntry | None


def snapshot_at(frame_id: str, at: datetime, *, position: PositionEntry | None, picture_sent: PictureEntry | None,
                picture_kept: PictureEntry | None) -> TuningSnapshot:
    """Compose the Frame's snapshot at `at` from the readers' answers: `position`, the Frame's newest
    Position entry at or before `at`; `picture_sent`, the newest sent picture entry naming the Frame
    at or before `at` (what Central sent its port); `picture_kept`, the newest kept entry at or
    before `at` of the Display seen on the Frame's port now. The picture is `picture_sent`, else
    `picture_kept`, else None. ValueError("snapshot_entry") when a given entry is after `at`, a
    Position entry is another Frame's, or `picture_sent` names another Frame or is not a sent kind,
    or `picture_kept` is not a kept kind."""
    if any(entry is not None and entry.at > at for entry in (position, picture_sent, picture_kept)):
        raise ValueError("snapshot_entry")
    if position is not None and position.frame_id != frame_id:
        raise ValueError("snapshot_entry")
    if picture_sent is not None and (picture_sent.frame_id != frame_id or picture_sent.record.kind not in SENT_KINDS):
        raise ValueError("snapshot_entry")
    if picture_kept is not None and picture_kept.record.kind not in KEPT_KINDS:
        raise ValueError("snapshot_entry")
    return TuningSnapshot(frame_id, at, position, picture_sent if picture_sent is not None else picture_kept)


__all__ = ["TuningSnapshot", "snapshot_at"]
