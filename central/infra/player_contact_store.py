"""A Player's liveness (migrations 115, 125), the one home of writing and reading it. Liveness is
its own domain, never read from another's signals.

A Player is live while its session socket is open, and for SILENT_AFTER_SECONDS after it closes
(contracts/liveness.py). Central writes the row twice per socket, through the Player's own
authentication: when the socket opens (`opened_in`: connected, heard now) and when it closes
(`closed_in`: not connected, heard now). Nothing is written between, so an idle Player costs no
write. One Central API process holds every Player socket, so at its start every row is cleared
(`disconnected_all_in`): a Central that stopped without closing its sockets never leaves a Player
reading live. Both times are Central's clock.
"""
from __future__ import annotations

from central.installation_models import PlayerReports


def _contact_in(conn, player_id: str, authority_epoch: int, now: float, connected: bool) -> None:
    conn.execute(
        "INSERT INTO player_contacts(player_id,authority_epoch,created_at,heard_at,connected) "
        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(player_id) DO UPDATE SET "
        "authority_epoch=EXCLUDED.authority_epoch,heard_at=EXCLUDED.heard_at,"
        "connected=EXCLUDED.connected",
        (player_id, authority_epoch, now, now, connected))


def opened_in(conn, player_id: str, authority_epoch: int, now: float) -> None:
    """`player_id`'s session socket on `authority_epoch` opened at `now`: connected, heard now."""
    _contact_in(conn, player_id, authority_epoch, now, True)


def closed_in(conn, player_id: str, authority_epoch: int, now: float) -> None:
    """`player_id`'s session socket on `authority_epoch` closed at `now`: not connected, heard
    now. A newer enrollment's row (another epoch) is left as it is."""
    conn.execute(
        "UPDATE player_contacts SET heard_at=%s, connected=FALSE "
        "WHERE player_id=%s AND authority_epoch=%s", (now, player_id, authority_epoch))


def disconnected_all_in(conn) -> None:
    """At the API process's start, before it accepts a socket: no Player socket is open."""
    conn.execute("UPDATE player_contacts SET connected=FALSE WHERE connected")


def player_reports_in(conn, read_at: float) -> PlayerReports:
    """When Central last heard from each non-retired Player on its current authority epoch,
    labelled with its read time; a Player whose socket is open is heard at `read_at`. From the
    caller's transaction, taking no lock."""
    rows = conn.execute(
        "SELECT c.player_id, c.heard_at, c.connected FROM players p JOIN player_contacts c "
        "ON c.player_id=p.id AND c.authority_epoch=p.authority_epoch WHERE p.retired_at IS NULL"
    ).fetchall()
    read_at = max((read_at, *(row["heard_at"] for row in rows)))
    reports = {row["player_id"]: read_at if row["connected"] else row["heard_at"] for row in rows}
    return PlayerReports(read_at=read_at, reports=reports)


def heard_at_in(conn, player_id: str, authority_epoch: int, now: float) -> float | None:
    """When Central last heard from `player_id` on `authority_epoch` (`now` while its socket is
    open); None when never."""
    row = conn.execute("SELECT heard_at, connected FROM player_contacts WHERE player_id=%s "
                       "AND authority_epoch=%s", (player_id, authority_epoch)).fetchone()
    if row is None:
        return None
    return now if row["connected"] else row["heard_at"]


__all__ = ["closed_in", "disconnected_all_in", "heard_at_in", "opened_in", "player_reports_in"]
