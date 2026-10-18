"""A Frame's Position history in PostgreSQL (migrations 096 and 099: position_entries). The rules are central/fleet/position_history.py's; this module appends, trims
and reads, nothing else.

Writers (each in its caller's transaction, so a history write fails only with its change):
- `record_created_in`: Registry's new Frame (central/registry.py `create_frame`) appends its
  KEPT_CREATED entry with the row it inserts.
- `write_position_in`: the one kept Position write, called by Registry's Save
  (central/registry.py `commit_calibration_trial_in`) and profile change (`replace_frame_profile`):
  it records a change made outside Photo Wall, updates the Frame's calibration and lighting (and
  profile), and appends the kept entry together, so no kept write can skip its entry.
- `record_sent_in`: the trial owner's `deliver_in` (central/fleet/node_calibration.py), on every
  exchange it hands a Frame: it compares without a lock and appends only when
  `position_history.sent_record` says so, re-checking under the lock.
Every append takes the Frame's row lock (FOR NO KEY UPDATE; a no-op under Registry's FOR UPDATE),
numbers the entry the Frame's newest + 1, stamps `at` with `clock_timestamp()` in the INSERT, reads
what it was made for (frames.profile and the Display seen on the Frame's bound port, display_store
SEEN_DISPLAY_JOIN), and trims the Frame's kept kinds to KEPT_ENTRIES and its sent kinds to
SENT_ENTRIES (central/tuning_history/bounds.py).
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb

from central.fleet.position_history import (
    KEPT_KINDS,
    SENT_KINDS,
    MadeFor,
    PositionEntry,
    PositionEntryKind,
    PositionRecord,
    PositionValues,
    TrialHanded,
    position_values,
    sent_record,
    unrecorded_record,
)
from central.infra.display_store import SEEN_DISPLAY_JOIN
from central.tuning_history import bounds as retention
from central.tuning_history.bounds import HistoryPage
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration, FrameProfile, RectangleFit

_KEPT_VALUES: Final = sorted(kind.value for kind in KEPT_KINDS)
_SENT_VALUES: Final = sorted(kind.value for kind in SENT_KINDS)
_COLUMNS: Final = ("e.id, e.frame_id, e.number, e.kind, e.at, e.session_id, e.restored_from, e.fit, e.lighting, "
                   "e.profile_width_px, e.profile_height_px, e.display_id")


def frame_made_for_in(conn: Any, frame_id: str) -> MadeFor | None:
    """What an entry for Frame `frame_id` is made for now: its profile in pixels and the Display seen
    on its bound port (None: unbound, or none seen). None when there is no such Frame. The one read
    of it: every append, Restore's refusal and the history's `restore_refusal`."""
    row = conn.execute("SELECT f.profile, seen.display_id FROM frames f LEFT JOIN bindings b ON b.frame_id=f.id "
                       f"{SEEN_DISPLAY_JOIN} WHERE f.id=%s", (frame_id,)).fetchone()
    if row is None:
        return None
    return MadeFor((int(row["profile"]["width_px"]), int(row["profile"]["height_px"])), row["display_id"])


def write_position_in(conn: Any, frame_id: str, record: PositionRecord, *, calibration: Calibration,
                      profile: FrameProfile | None = None) -> int:
    """Store `calibration` and `record`'s lighting on Frame `frame_id` (and `profile`, a new generation,
    for a profile change) and append `record` as its kept entry, in the caller's transaction: the one
    kept Position write (Save: KEPT_SAVED, no `profile`; a profile change: KEPT_PROFILE_CHANGED with
    it). First, when the stored fit and lighting differ from the Frame's newest kept entry, the change made
    outside Photo Wall is recorded (`position_history.unrecorded_record`). Answers the
    Frame's generation after the write. ValueError("position_history_kind") for another kind or a
    `profile` that does not match it; ValueError("position_history_values") when `record`'s fit is not
    `calibration`'s."""
    if record.kind not in (PositionEntryKind.KEPT_SAVED, PositionEntryKind.KEPT_PROFILE_CHANGED) or (
            (profile is None) != (record.kind is PositionEntryKind.KEPT_SAVED)):
        raise ValueError("position_history_kind")
    if record.values.fit != calibration.fit:
        raise ValueError("position_history_values")
    stored = conn.execute("SELECT calibration, lighting FROM frames WHERE id=%s FOR NO KEY UPDATE", (frame_id,)).fetchone()
    if stored is None:
        raise LookupError("unknown_frame")
    unrecorded = unrecorded_record(latest_kept_position_in(conn, frame_id), position_values(
        Calibration.model_validate(stored["calibration"]), FrameLighting.model_validate(stored["lighting"])))
    if unrecorded is not None:
        _append_in(conn, frame_id, unrecorded)
    sets = ["calibration=%s", "lighting=%s", "configuration_revision=configuration_revision+1"]
    params = [Jsonb(calibration.model_dump(mode="json")), Jsonb(record.values.lighting.model_dump(mode="json"))]
    if profile is not None:
        sets += ["profile=%s", "generation=generation+1"]
        params.append(Jsonb(profile.model_dump()))
    generation = conn.execute(f"UPDATE frames SET {','.join(sets)} WHERE id=%s RETURNING generation",  # noqa: S608
                              (*params, frame_id)).fetchone()["generation"]
    _append_in(conn, frame_id, record)
    return generation


def record_created_in(conn: Any, frame_id: str, record: PositionRecord) -> None:
    """Append a new Frame's KEPT_CREATED `record`, in the caller's transaction that inserted the Frame.
    ValueError("position_history_kind") for another kind."""
    if record.kind is not PositionEntryKind.KEPT_CREATED:
        raise ValueError("position_history_kind")
    _append_in(conn, frame_id, record)


def _append_in(conn: Any, frame_id: str, record: PositionRecord) -> PositionEntry:
    """Append `record` as the Frame's next entry, made for its profile and seen Display now, and trim."""
    conn.execute("SELECT 1 FROM frames WHERE id=%s FOR NO KEY UPDATE", (frame_id,))
    made_for = frame_made_for_in(conn, frame_id)
    if made_for is None:
        raise LookupError("unknown_frame")
    width, height = made_for.profile_px
    values = record.values
    row = conn.execute(
        "INSERT INTO position_entries AS e (id, frame_id, number, kind, at, session_id, restored_from, fit, lighting, "
        "profile_width_px, profile_height_px, display_id) SELECT %s, %s, COALESCE(max(number), 0) + 1, %s, "
        "clock_timestamp(), %s, %s, %s, %s, %s, %s, %s FROM position_entries WHERE frame_id=%s "
        f"RETURNING {_COLUMNS}",
        (uuid.uuid4(), frame_id, record.kind.value, record.session_id, record.restored_from,
         Jsonb(values.fit.model_dump(mode="json")), Jsonb(values.lighting.model_dump(mode="json")), width, height,
         made_for.display_id, frame_id)).fetchone()
    kinds, keep = ((_KEPT_VALUES, retention.KEPT_ENTRIES) if record.kind in KEPT_KINDS
                   else (_SENT_VALUES, retention.SENT_ENTRIES))
    conn.execute("DELETE FROM position_entries WHERE frame_id=%(frame)s AND kind = ANY(%(kinds)s) "
                 "AND number <= (SELECT number FROM position_entries WHERE frame_id=%(frame)s "
                 "AND kind = ANY(%(kinds)s) ORDER BY number DESC OFFSET %(keep)s LIMIT 1)",
                 {"frame": frame_id, "kinds": kinds, "keep": keep})
    return _entry(row)


def record_sent_in(conn: Any, frame_id: str, *, trial: TrialHanded | None, saved: PositionValues) -> None:
    """After an exchange decides what it hands the Node for the Frame (`trial`, or None: the kept
    values `saved`): append `position_history.sent_record(...)` against the Frame's newest entry when
    it is not None. Takes the Frame's row lock only to append (and re-reads the newest entry under
    it), so an exchange that changes nothing locks nothing."""
    if sent_record(trial=trial, saved=saved, latest=latest_position_in(conn, frame_id)) is None:
        return
    conn.execute("SELECT 1 FROM frames WHERE id=%s FOR NO KEY UPDATE", (frame_id,))
    record = sent_record(trial=trial, saved=saved, latest=latest_position_in(conn, frame_id))
    if record is not None:
        _append_in(conn, frame_id, record)


def latest_position_in(conn: Any, frame_id: str) -> PositionEntry | None:
    """The Frame's newest entry of any kind (by number); None when it has none."""
    return _one(conn, f"SELECT {_COLUMNS} FROM position_entries e WHERE e.frame_id=%s "  # noqa: S608
                      "ORDER BY e.number DESC LIMIT 1", (frame_id,))


def latest_kept_position_in(conn: Any, frame_id: str) -> PositionEntry | None:
    """The Frame's newest kept entry (by number); None when it has none."""
    return _one(conn, f"SELECT {_COLUMNS} FROM position_entries e WHERE e.frame_id=%s "  # noqa: S608
                      "AND e.kind = ANY(%s) ORDER BY e.number DESC LIMIT 1", (frame_id, _KEPT_VALUES))


def position_entry_in(conn: Any, entry_id: UUID) -> PositionEntry | None:
    """Entry `entry_id`; None when there is none (or it was trimmed)."""
    return _one(conn, f"SELECT {_COLUMNS} FROM position_entries e WHERE e.id=%s", (entry_id,))  # noqa: S608


def position_entries_in(conn: Any, frame_id: str, page: HistoryPage) -> tuple[PositionEntry, ...]:
    """One page of the Frame's entries, newest first by `at` then number."""
    before = "" if page.before is None else " AND e.at < %(before)s"
    rows = conn.execute(f"SELECT {_COLUMNS} FROM position_entries e WHERE e.frame_id=%(frame)s{before} "  # noqa: S608
                        "ORDER BY e.at DESC, e.number DESC LIMIT %(limit)s",
                        {"frame": frame_id, "before": page.before, "limit": page.limit}).fetchall()
    return tuple(_entry(row) for row in rows)


def latest_position_at_in(conn: Any, frame_id: str, at: datetime) -> PositionEntry | None:
    """The Frame's newest entry with `at` at or before `at`; None when none."""
    return _one(conn, f"SELECT {_COLUMNS} FROM position_entries e WHERE e.frame_id=%s AND e.at <= %s "  # noqa: S608
                      "ORDER BY e.at DESC, e.number DESC LIMIT 1", (frame_id, at))


def _one(conn: Any, sql: str, params: tuple[Any, ...]) -> PositionEntry | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else _entry(row)


def _entry(row: Mapping[str, Any]) -> PositionEntry:
    """The entry of a row read under `_COLUMNS`, `at` in UTC."""
    return PositionEntry(
        row["id"], row["frame_id"], row["number"], row["at"].astimezone(UTC),
        MadeFor((row["profile_width_px"], row["profile_height_px"]), row["display_id"]),
        PositionRecord(PositionEntryKind(row["kind"]),
                       PositionValues(RectangleFit.model_validate(row["fit"]), FrameLighting.model_validate(row["lighting"])),
                       row["session_id"], row["restored_from"]))


__all__ = ["frame_made_for_in", "latest_kept_position_in", "latest_position_at_in", "latest_position_in",
           "position_entries_in", "position_entry_in", "record_created_in", "record_sent_in", "write_position_in"]
