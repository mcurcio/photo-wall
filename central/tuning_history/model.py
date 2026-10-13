"""The configuration snapshot's rules, pure: retention, the history page, Central's time as the routes
read it, and the one rule that composes a Frame's snapshot at a minute from its owners' histories.

Retention (design, "Retention and failure behaviour"; placeholders the owner may tune): per key, the
newest KEPT_ENTRIES kept and SENT_ENTRIES sent entries; each writer trims its own key under its own
lock when it appends, so an idle key stays bounded and there is no reaper.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from central.displays.picture_history import KEPT_KINDS, SENT_KINDS, PictureEntry
from central.fleet.position_history import PositionEntry

KEPT_ENTRIES: Final = 500           # per Display (picture) and per Frame (Position): kept entries kept
SENT_ENTRIES: Final = 20_000        # per port (picture) and per Frame (Position): ~1 h of dragging at ~5 a second
PAGE_LIMIT_DEFAULT: Final = 100     # a history page's entries when the reader names no limit
PAGE_LIMIT_MAX: Final = 500         # the most one page answers


class HistoryRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`: unknown_display 404,
    unknown_frame 404, bad_range 422 (a page limit outside 1..PAGE_LIMIT_MAX), bad_time 422 (a time
    that is not ISO 8601 with its zone)."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class HistoryPage:
    """One page of a history, newest first by `at` then number: entries strictly before `before`
    (aware UTC; None: from the newest), at most `limit` of them."""
    before: datetime | None
    limit: int


def utc_instant(text: str) -> datetime:
    """`text`, an ISO 8601 time with its zone ("2026-10-13T21:04:07Z", "...+01:00"), as an aware
    datetime in UTC. HistoryRefused("bad_time", 422) for anything else; a time with no zone is
    refused, never guessed."""
    try:
        instant = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise HistoryRefused("bad_time", 422) from None
    if instant.utcoffset() is None:
        raise HistoryRefused("bad_time", 422)
    return instant.astimezone(UTC)


def history_page(before: str | None, limit: int | None) -> HistoryPage:
    """The page a route's query names: `before` through `utc_instant` (None: the newest), `limit`
    PAGE_LIMIT_DEFAULT when None. HistoryRefused("bad_time", 422), ("bad_range", 422) for a limit
    outside 1..PAGE_LIMIT_MAX."""
    instant = None if before is None else utc_instant(before)
    if limit is None:
        limit = PAGE_LIMIT_DEFAULT
    elif type(limit) is not int or not 1 <= limit <= PAGE_LIMIT_MAX:
        raise HistoryRefused("bad_range", 422)
    return HistoryPage(instant, limit)


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


__all__ = ["KEPT_ENTRIES", "PAGE_LIMIT_DEFAULT", "PAGE_LIMIT_MAX", "SENT_ENTRIES", "HistoryPage",
           "HistoryRefused", "TuningSnapshot", "history_page", "snapshot_at", "utc_instant"]
