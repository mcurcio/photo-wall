"""A Frame's Position history in PostgreSQL (configuration snapshots; migration 096:
position_entries). The rules are central/fleet/position_history.py's; this module appends, trims
and reads, nothing else.

Writers (each in its caller's transaction, so a history write fails only with its change):
- `record_unrecorded_in` then `append_position_in`: Registry's two kept writes
  (central/registry.py `commit_calibration_trial_in`, `replace_frame_profile`), the first before
  their UPDATE (so an unrecorded entry is made for what was there), the second after it.
- `record_sent_in`: the trial owner's `deliver_in` (central/fleet/node_calibration.py), on every
  exchange it hands a Frame: it compares without a lock and appends only when
  `position_history.sent_record` says so, re-checking under the lock.
Every append takes the Frame's row lock (FOR NO KEY UPDATE; a no-op under Registry's FOR UPDATE),
numbers the entry the Frame's newest + 1, stamps `at` with `clock_timestamp()` in the INSERT, reads
what it was made for (frames.profile and the Display seen on the Frame's bound port, display_store
SEEN_DISPLAY_JOIN), and trims the Frame's kept kinds to KEPT_ENTRIES and its sent kinds to
SENT_ENTRIES (central/tuning_history/model.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from central.fleet.position_history import (
    PositionEntry,
    PositionRecord,
    PositionValues,
    TrialHanded,
)
from central.tuning_history.model import HistoryPage


def append_position_in(conn: Any, frame_id: str, record: PositionRecord) -> PositionEntry:
    """Append `record` as the Frame's next entry, made for its profile and seen Display now, and trim."""
    ...


def record_unrecorded_in(conn: Any, frame_id: str, stored: PositionValues) -> None:
    """Before a kept write: append `position_history.unrecorded_record(latest kept, stored)` when it
    is not None (`stored`: the Frame's fit and mat as they are before the write)."""
    ...


def record_sent_in(conn: Any, frame_id: str, *, trial: TrialHanded | None, saved: PositionValues) -> None:
    """After an exchange decides what it hands the Node for the Frame (`trial`, or None: the kept
    values `saved`): append `position_history.sent_record(...)` against the Frame's newest entry when
    it is not None. Takes the Frame's row lock only to append (and re-reads the newest entry under
    it), so an exchange that changes nothing locks nothing."""
    ...


def latest_position_in(conn: Any, frame_id: str) -> PositionEntry | None:
    """The Frame's newest entry of any kind (by number); None when it has none."""
    ...


def latest_kept_position_in(conn: Any, frame_id: str) -> PositionEntry | None:
    """The Frame's newest kept entry (by number); None when it has none."""
    ...


def position_entry_in(conn: Any, entry_id: UUID) -> PositionEntry | None:
    """Entry `entry_id`; None when there is none (or it was trimmed)."""
    ...


def position_entries_in(conn: Any, frame_id: str, page: HistoryPage) -> tuple[PositionEntry, ...]:
    """One page of the Frame's entries, newest first by `at` then number."""
    ...


def latest_position_at_in(conn: Any, frame_id: str, at: datetime) -> PositionEntry | None:
    """The Frame's newest entry with `at` at or before `at`; None when none."""
    ...


__all__ = ["append_position_in", "latest_kept_position_in", "latest_position_at_in", "latest_position_in",
           "position_entries_in", "position_entry_in", "record_sent_in", "record_unrecorded_in"]
