"""The configuration snapshot's reads over PostgreSQL (`TuningHistoryQueries`; migration 096). Each
method runs in one transaction and reads through its owner's store (picture_history_store,
position_history_store); names (the Display's, the Frame's) are read today. It never
writes.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from central.db import Database
from central.displays.picture_history import PictureEntry, Port, unrecorded_record
from central.displays.views import (
    DisplayRefView,
    PictureEntryView,
    PictureHistoryView,
    picture_entry_view,
)
from central.fleet import position_history
from central.fleet.position_history import MadeFor, PositionEntry, position_values
from central.fleet.position_views import PositionEntryView, PositionHistoryView, position_entry_view
from central.infra.picture_columns import PICTURE_COLUMNS, stored_picture
from central.infra.picture_history_store import (
    latest_kept_at_in,
    latest_kept_in,
    latest_sent_for_frame_at_in,
    picture_entries_in,
    sent_entry_in,
)
from central.infra.position_history_store import (
    frame_made_for_in,
    latest_kept_position_in,
    latest_position_at_in,
    position_entries_in,
)
from central.installation_models import FrameRefView
from central.tuning_history import model
from central.tuning_history.bounds import HistoryPage, HistoryRefused
from central.tuning_history.views import TuningSnapshotView
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration


class PgTuningHistory:
    """`TuningHistoryQueries` over PostgreSQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def picture_history(self, display_id: UUID, page: HistoryPage) -> PictureHistoryView:
        """HistoryRefused("unknown_display", 404). `unrecorded_change` is
        `picture_history.unrecorded_record(latest kept, stored) is not None`."""
        with self._db.transaction() as conn:
            display = conn.execute(f"SELECT d.name, {PICTURE_COLUMNS} FROM displays d WHERE d.id=%s",  # noqa: S608
                                   (display_id,)).fetchone()
            if display is None:
                raise HistoryRefused("unknown_display", 404)
            unrecorded = unrecorded_record(latest_kept_in(conn, display_id), stored_picture(display)) is not None
            entries = _picture_views_in(conn, picture_entries_in(conn, display_id, page))
        return PictureHistoryView(display=DisplayRefView(id=display_id, name=display["name"]),
                                  unrecorded_change=unrecorded, entries=entries)

    def position_history(self, frame_id: str, page: HistoryPage) -> PositionHistoryView:
        """HistoryRefused("unknown_frame", 404). Each entry's `restore_refusal` is
        `position_history.restore_refusal` against the Frame's profile and the Display seen on its
        port now; `unrecorded_change` is `position_history.unrecorded_record(latest kept, stored) is
        not None`."""
        with self._db.transaction() as conn:
            frame, made_for = _frame_in(conn, frame_id)
            stored = position_values(Calibration.model_validate(frame["calibration"]), FrameLighting.model_validate(frame["lighting"]))
            unrecorded = position_history.unrecorded_record(latest_kept_position_in(conn, frame_id), stored) is not None
            entries = position_entries_in(conn, frame_id, page)
        return PositionHistoryView(frame=FrameRefView(id=frame_id, name=frame["name"]), unrecorded_change=unrecorded,
                                   entries=_position_views(entries, frame_id, frame["name"], made_for))

    def snapshot_at(self, frame_id: str, at: datetime) -> TuningSnapshotView:
        """HistoryRefused("unknown_frame", 404). `model.snapshot_at` over
        `latest_position_at_in`, `latest_sent_for_frame_at_in` and, for the Display seen on the
        Frame's port now (none: no kept read), `latest_kept_at_in`."""
        with self._db.transaction() as conn:
            frame, made_for = _frame_in(conn, frame_id)
            seen = made_for.display_id
            snapshot = model.snapshot_at(
                frame_id, at, position=latest_position_at_in(conn, frame_id, at),
                picture_sent=latest_sent_for_frame_at_in(conn, frame_id, at),
                picture_kept=None if seen is None else latest_kept_at_in(conn, seen, at))
            pictures = _picture_views_in(conn, () if snapshot.picture is None else (snapshot.picture,))
        positions = _position_views(() if snapshot.position is None else (snapshot.position,), frame_id,
                                    frame["name"], made_for)
        return TuningSnapshotView(frame=FrameRefView(id=frame_id, name=frame["name"]), at=snapshot.at,
                                  position=positions[0] if positions else None,
                                  picture=pictures[0] if pictures else None)

    def sent_picture_entry(self, player_id: str, output_id: str, picture_change: int) -> PictureEntryView:
        """HistoryRefused("unknown_entry", 404). The entry under picture_sent_entries' key
        (`sent_entry_in`), answered as the picture history answers it."""
        with self._db.transaction() as conn:
            entry = sent_entry_in(conn, Port(player_id, output_id), picture_change)
            if entry is None:
                raise HistoryRefused("unknown_entry", 404)
            return _picture_views_in(conn, (entry,))[0]


def _frame_in(conn: Any, frame_id: str) -> tuple[Any, MadeFor]:
    """The Frame's row (name, calibration, lighting) and what an entry is made for on it now.
    HistoryRefused("unknown_frame", 404)."""
    frame = conn.execute("SELECT f.name, f.calibration, f.lighting FROM frames f WHERE f.id=%s", (frame_id,)).fetchone()
    made_for = frame_made_for_in(conn, frame_id)
    if frame is None or made_for is None:
        raise HistoryRefused("unknown_frame", 404)
    return frame, made_for


def _picture_views_in(conn: Any, entries: Iterable[PictureEntry]) -> tuple[PictureEntryView, ...]:
    """`entries` as the routes answer them, with today's names of their Displays and their Frames (a
    deleted one: none)."""
    entries = tuple(entries)
    display_ids = sorted({entry.display_id for entry in entries}, key=str)
    frame_ids = sorted({entry.frame_id for entry in entries if entry.frame_id is not None})
    displays = {row["id"]: row["name"] for row in conn.execute(
        "SELECT id, name FROM displays WHERE id = ANY(%s)", (display_ids,)).fetchall()} if display_ids else {}
    frames = {row["id"]: row["name"] for row in conn.execute(
        "SELECT id, name FROM frames WHERE id = ANY(%s)", (frame_ids,)).fetchall()} if frame_ids else {}
    return tuple(picture_entry_view(
        entry, display_name=displays[entry.display_id],
        frame_name=None if entry.frame_id is None else frames.get(entry.frame_id))
        for entry in entries)


def _position_views(entries: Iterable[PositionEntry], frame_id: str, frame_name: str,
                    made_for: MadeFor) -> tuple[PositionEntryView, ...]:
    """`entries` (the Frame's) as the routes answer them, each with Restore's answer for it on the
    Frame now (`position_history.restore_refusal` against what an entry is made for there now)."""
    return tuple(position_entry_view(
        entry, frame_name=frame_name,
        restore_refusal=position_history.restore_refusal(entry, frame_id=frame_id, now=made_for))
        for entry in entries)


__all__ = ["PgTuningHistory"]
