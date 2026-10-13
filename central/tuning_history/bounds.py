"""The bounds every history shares, pure: retention, the history page and Central's time as the routes
read it. The lowest module of configuration snapshots: each owner's store (a Display's picture
history, a Frame's Position history) trims and pages by it, and it knows neither owner, so no owner
depends on the other or on the snapshot's composer (central/tuning_history/model.py).

Retention (design, "Retention and failure behaviour"; placeholders the owner may tune): per key, the
newest KEPT_ENTRIES kept and SENT_ENTRIES sent entries; each writer trims its own key under its own
lock when it appends, so an idle key stays bounded and there is no reaper.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

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


__all__ = ["KEPT_ENTRIES", "PAGE_LIMIT_DEFAULT", "PAGE_LIMIT_MAX", "SENT_ENTRIES", "HistoryPage",
           "HistoryRefused", "history_page", "utc_instant"]
