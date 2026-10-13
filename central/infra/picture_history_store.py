"""A Display's picture history in PostgreSQL (configuration snapshots; migration 096:
picture_kept_entries, picture_sent_entries). The rules are central/displays/picture_history.py's;
this module appends, trims and reads, nothing else.

Writers (each in its caller's transaction, so a history write fails only with its change):
- `append_kept_in`: called only by central/infra/display_store.py `write_picture` (Done, Copy), under
  the Display's row lock, which it takes itself too (FOR NO KEY UPDATE; a no-op under a caller's
  stronger lock): the number is the Display's newest + 1.
- `append_sent_in`: called only by display_store `output_documents` when a port's picture_change
  rises; idempotent on (player_id, output_id, picture_change).
Each trims its own key to the newest central/tuning_history/bounds.py KEPT_ENTRIES (per Display) or
SENT_ENTRIES (per port) after appending. Every `at` is `clock_timestamp()` in the INSERT
(PostgreSQL's clock). An entry's picture is stored under the displays columns' names
(picture_columns `picture_fields`, as picture_sessions.draft) with its LUT's recipe beside it.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb

from central.displays.picture_history import (
    KEPT_KINDS,
    SENT_KINDS,
    PictureEntry,
    PictureEntryKind,
    PictureRecord,
    Port,
)
from central.infra.picture_columns import picture_fields, stored_picture
from central.tuning_history import bounds as retention
from central.tuning_history.bounds import HistoryPage
from contracts.lut import LutRef
from contracts.tuning import TestPattern

# Both tables read under one shape, so every reader decodes an entry alike (`_entry`): a kept entry
# has no port, Frame or test pattern; a sent entry's number is its port's picture_change.
_KEPT: Final = ("SELECT k.id, k.display_id, k.number, k.kind, k.at, k.session_id, k.restored_from, k.picture, "
                "k.lut_recipe_id, NULL::text AS player_id, NULL::text AS output_id, NULL::text AS frame_id, "
                "NULL::text AS test_pattern FROM picture_kept_entries k")
_SENT: Final = ("SELECT s.id, s.display_id, s.picture_change AS number, s.kind, s.at, s.session_id, s.restored_from, "
                "s.picture, s.lut_recipe_id, s.player_id, s.output_id, s.frame_id, s.test_pattern "
                "FROM picture_sent_entries s")


def append_kept_in(conn: Any, display_id: UUID, record: PictureRecord) -> PictureEntry:
    """Append `record` (a kept kind) as the Display's next kept entry and trim the Display to
    KEPT_ENTRIES. ValueError("picture_history_kind") for a sent kind."""
    if record.kind not in KEPT_KINDS:
        raise ValueError("picture_history_kind")
    conn.execute("SELECT 1 FROM displays WHERE id=%s FOR NO KEY UPDATE", (display_id,))
    lut = record.picture.software.lut
    row = conn.execute(
        "INSERT INTO picture_kept_entries AS k (id, display_id, number, kind, at, session_id, restored_from, "
        "picture, lut_recipe_id) SELECT %s, %s, COALESCE(max(number), 0) + 1, %s, clock_timestamp(), %s, %s, %s, %s "
        "FROM picture_kept_entries WHERE display_id=%s "
        "RETURNING k.id, k.display_id, k.number, k.kind, k.at, k.session_id, k.restored_from, k.picture, "
        "k.lut_recipe_id, NULL::text AS player_id, NULL::text AS output_id, NULL::text AS frame_id, "
        "NULL::text AS test_pattern",
        (uuid.uuid4(), display_id, record.kind.value, record.session_id, record.restored_from,
         Jsonb(picture_fields(record.picture)), None if lut is None else lut.recipe_id, display_id)).fetchone()
    conn.execute("DELETE FROM picture_kept_entries WHERE display_id=%s AND number <= (SELECT number "
                 "FROM picture_kept_entries WHERE display_id=%s ORDER BY number DESC OFFSET %s LIMIT 1)",
                 (display_id, display_id, retention.KEPT_ENTRIES))
    return _entry(row)


def append_sent_in(conn: Any, port: Port, picture_change: int, *, display_id: UUID, frame_id: str | None,
                   record: PictureRecord) -> None:
    """Append `record` (a sent kind) as the port's entry for `picture_change`, naming the Display seen
    there and the Frame bound to it (None: unbound); nothing when the port already has an entry for
    that change. Trims the port to SENT_ENTRIES. ValueError("picture_history_kind") for a kept kind."""
    if record.kind not in SENT_KINDS:
        raise ValueError("picture_history_kind")
    lut, pattern = record.picture.software.lut, record.test_pattern
    conn.execute(
        "INSERT INTO picture_sent_entries(id, player_id, output_id, picture_change, display_id, frame_id, kind, at, "
        "session_id, restored_from, picture, lut_recipe_id, test_pattern) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp(), %s, %s, %s, %s, %s) "
        "ON CONFLICT (player_id, output_id, picture_change) DO NOTHING",
        (uuid.uuid4(), port.player_id, port.output_id, picture_change, display_id, frame_id, record.kind.value,
         record.session_id, record.restored_from, Jsonb(picture_fields(record.picture)),
         None if lut is None else lut.recipe_id, None if pattern is None else pattern.value))
    conn.execute("DELETE FROM picture_sent_entries WHERE player_id=%(player)s AND output_id=%(output)s "
                 "AND picture_change <= (SELECT picture_change FROM picture_sent_entries WHERE player_id=%(player)s "
                 "AND output_id=%(output)s ORDER BY picture_change DESC OFFSET %(keep)s LIMIT 1)",
                 {"player": port.player_id, "output": port.output_id, "keep": retention.SENT_ENTRIES})


def latest_kept_in(conn: Any, display_id: UUID) -> PictureEntry | None:
    """The Display's newest kept entry (by number); None when it has none."""
    return _one(conn, _KEPT + " WHERE k.display_id=%s ORDER BY k.number DESC LIMIT 1", (display_id,))


def latest_sent_in(conn: Any, port: Port) -> PictureEntry | None:
    """The port's newest sent entry (by picture_change); None when it has none."""
    return _one(conn, _SENT + " WHERE s.player_id=%s AND s.output_id=%s ORDER BY s.picture_change DESC LIMIT 1",
                (port.player_id, port.output_id))


def sent_entry_in(conn: Any, port: Port, picture_change: int) -> PictureEntry | None:
    """The port's sent entry for `picture_change` (the table's key); None when it has none (never
    sent, or trimmed)."""
    return _one(conn, _SENT + " WHERE s.player_id=%s AND s.output_id=%s AND s.picture_change=%s",
                (port.player_id, port.output_id, picture_change))


def picture_entry_in(conn: Any, entry_id: UUID) -> PictureEntry | None:
    """The kept or sent entry `entry_id`; None when there is none (or it was trimmed)."""
    return _one(conn, f"{_KEPT} WHERE k.id=%s UNION ALL {_SENT} WHERE s.id=%s", (entry_id, entry_id))


def picture_entries_in(conn: Any, display_id: UUID, page: HistoryPage) -> tuple[PictureEntry, ...]:
    """One page of the Display's kept and sent entries together, newest first by `at` then number."""
    before_kept, before_sent = ("", "") if page.before is None else (" AND k.at < %(before)s", " AND s.at < %(before)s")
    rows = conn.execute(
        f"{_KEPT} WHERE k.display_id=%(display)s{before_kept} UNION ALL "  # noqa: S608
        f"{_SENT} WHERE s.display_id=%(display)s{before_sent} ORDER BY at DESC, number DESC LIMIT %(limit)s",
        {"display": display_id, "before": page.before, "limit": page.limit}).fetchall()
    return tuple(_entry(row) for row in rows)


def latest_sent_for_frame_at_in(conn: Any, frame_id: str, at: datetime) -> PictureEntry | None:
    """The newest sent entry naming Frame `frame_id` with `at` at or before `at`; None when none."""
    return _one(conn, _SENT + " WHERE s.frame_id=%s AND s.at <= %s ORDER BY s.at DESC, s.picture_change DESC LIMIT 1",
                (frame_id, at))


def latest_kept_at_in(conn: Any, display_id: UUID, at: datetime) -> PictureEntry | None:
    """The Display's newest kept entry with `at` at or before `at`; None when none."""
    return _one(conn, _KEPT + " WHERE k.display_id=%s AND k.at <= %s ORDER BY k.at DESC, k.number DESC LIMIT 1",
                (display_id, at))


def _one(conn: Any, sql: str, params: tuple[Any, ...]) -> PictureEntry | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else _entry(row)


def _entry(row: Mapping[str, Any]) -> PictureEntry:
    """The entry of a row read under `_KEPT`'s or `_SENT`'s shape: its picture with the LUT under the
    recipe recorded beside it (the stored picture names only the paper profile), `at` in UTC."""
    picture = stored_picture(row["picture"])
    lut, recipe = picture.software.lut, row["lut_recipe_id"]
    if lut is not None and recipe is not None:
        picture = replace(picture, software=replace(picture.software, lut=LutRef(lut.profile_sha256, recipe)))
    pattern = row["test_pattern"]
    return PictureEntry(
        row["id"], row["display_id"], row["number"], row["at"].astimezone(UTC),
        None if row["player_id"] is None else Port(row["player_id"], row["output_id"]), row["frame_id"],
        PictureRecord(PictureEntryKind(row["kind"]), picture, row["session_id"], row["restored_from"],
                      None if pattern is None else TestPattern(pattern)))


__all__ = ["append_kept_in", "append_sent_in", "latest_kept_at_in", "latest_kept_in", "latest_sent_for_frame_at_in",
           "latest_sent_in", "picture_entries_in", "picture_entry_in", "sent_entry_in"]
