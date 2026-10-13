"""A Display's picture history in PostgreSQL (configuration snapshots; migration 096:
picture_kept_entries, picture_sent_entries). The rules are central/displays/picture_history.py's;
this module appends, trims and reads, nothing else.

Writers (each in its caller's transaction, so a history write fails only with its change):
- `append_kept_in`: called only by central/infra/display_store.py `write_picture` (Done, Copy), under
  the Display's row lock, which it takes itself too (FOR NO KEY UPDATE; a no-op under a caller's
  stronger lock): the number is the Display's newest + 1.
- `append_sent_in`: called only by display_store `output_documents` when a port's picture_change
  rises; idempotent on (player_id, output_id, picture_change).
Each trims its own key to the newest central/tuning_history/model.py KEPT_ENTRIES (per Display) or
SENT_ENTRIES (per port) after appending. Every `at` is `clock_timestamp()` in the INSERT
(PostgreSQL's clock). An entry's picture is stored under the displays columns' names
(display_store `picture_fields`, as picture_sessions.draft) with its LUT's recipe beside it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from central.displays.picture_history import PictureEntry, PictureRecord, Port
from central.tuning_history.model import HistoryPage


def append_kept_in(conn: Any, display_id: UUID, record: PictureRecord) -> PictureEntry:
    """Append `record` (a kept kind) as the Display's next kept entry and trim the Display to
    KEPT_ENTRIES. ValueError("picture_history_kind") for a sent kind."""
    ...


def append_sent_in(conn: Any, port: Port, picture_change: int, *, display_id: UUID, frame_id: str | None,
                   record: PictureRecord) -> None:
    """Append `record` (a sent kind) as the port's entry for `picture_change`, naming the Display seen
    there and the Frame bound to it (None: unbound); nothing when the port already has an entry for
    that change. Trims the port to SENT_ENTRIES. ValueError("picture_history_kind") for a kept kind."""
    ...


def latest_kept_in(conn: Any, display_id: UUID) -> PictureEntry | None:
    """The Display's newest kept entry (by number); None when it has none."""
    ...


def latest_sent_in(conn: Any, port: Port) -> PictureEntry | None:
    """The port's newest sent entry (by picture_change); None when it has none."""
    ...


def picture_entry_in(conn: Any, entry_id: UUID) -> PictureEntry | None:
    """The kept or sent entry `entry_id`; None when there is none (or it was trimmed)."""
    ...


def picture_entries_in(conn: Any, display_id: UUID, page: HistoryPage) -> tuple[PictureEntry, ...]:
    """One page of the Display's kept and sent entries together, newest first by `at` then number."""
    ...


def latest_sent_for_frame_at_in(conn: Any, frame_id: str, at: datetime) -> PictureEntry | None:
    """The newest sent entry naming Frame `frame_id` with `at` at or before `at`; None when none."""
    ...


def latest_kept_at_in(conn: Any, display_id: UUID, at: datetime) -> PictureEntry | None:
    """The Display's newest kept entry with `at` at or before `at`; None when none."""
    ...


__all__ = ["append_kept_in", "append_sent_in", "latest_kept_at_in", "latest_kept_in", "latest_sent_for_frame_at_in",
           "latest_sent_in", "picture_entries_in", "picture_entry_in"]
