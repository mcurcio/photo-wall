"""The Home Assistant link's health: derived from a heartbeat's age, never stored as a transition, so
a worker that dies without saying so reads "away" once its lease runs out.

The worker's link beats every HEARTBEAT_SECONDS with what it is doing now (`Beat`); a reader judges
the newest beat by its age on PostgreSQL's one clock (`judged`). No beat at all: the worker never
ran a link since the table was made.
"""
from __future__ import annotations

from typing import Final, Literal

from central.db import Database
from contracts.models import Instant, Model

HEARTBEAT_SECONDS: Final = 5.0
LEASE_SECONDS: Final = 3 * HEARTBEAT_SECONDS     # a beat older than this is a link that is away

Beat = Literal["not-configured", "connecting", "connected"]   # what the worker's link says it is doing
LinkState = Literal["not-configured", "connecting", "connected", "away", "never-run"]


class LinkHealthView(Model):
    """The link as the Home Assistant settings page shows it: its state and since when it has been
    in it (Central's clock; None for never-run)."""
    state: LinkState
    since: Instant | None


def judged(beat: Beat | None, since: float | None, age_seconds: float | None) -> LinkHealthView:
    """The link's health: "never-run" with no beat; "away" when the newest beat is older than
    LEASE_SECONDS (since None: only the age is known); otherwise the beat's own state since
    `since`. Pure."""
    if beat is None or since is None or age_seconds is None:
        return LinkHealthView(state="never-run", since=None)
    if age_seconds > LEASE_SECONDS:
        return LinkHealthView(state="away", since=None)
    return LinkHealthView(state=beat, since=since)


class PgLinkHealth:
    """The heartbeat over one Database (table home_assistant_link, migration 099)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def beat(self, beat: Beat) -> None:
        """Record `beat` now (clock_timestamp()); `since` moves only when the state changes."""
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO home_assistant_link(one, beat, since, beat_at) "
                         "VALUES (true, %s, clock_timestamp(), clock_timestamp()) ON CONFLICT (one) DO UPDATE "
                         "SET beat=EXCLUDED.beat, beat_at=EXCLUDED.beat_at, since=CASE WHEN home_assistant_link.beat="
                         "EXCLUDED.beat THEN home_assistant_link.since ELSE EXCLUDED.since END", (beat,))

    def health(self) -> LinkHealthView:
        """`judged` over the newest beat and its age on the database's clock."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT beat, EXTRACT(EPOCH FROM since) AS since, "
                               "EXTRACT(EPOCH FROM clock_timestamp() - beat_at) AS age "
                               "FROM home_assistant_link WHERE one").fetchone()
        if row is None:
            return judged(None, None, None)
        return judged(row["beat"], float(row["since"]), float(row["age"]))


__all__ = ["HEARTBEAT_SECONDS", "LEASE_SECONDS", "Beat", "LinkHealthView", "LinkState", "PgLinkHealth", "judged"]
