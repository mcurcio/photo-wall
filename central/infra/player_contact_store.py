"""A Player's liveness: when Central last heard from it (migration 115), the one home of writing
and reading it. Liveness is its own domain, never read from another's signals: until 2026-11-16 it
was the readiness report's receipt, so a Player had to post readiness every half second, a commit
under the fleet-wide Coordination lock each, only to read as live.

Heard is any request the Player authenticates (`Registry.authenticate`: its state poll every
REPORT_INTERVAL, its clock probe, its reports), written at most every HEARD_WRITE_SECONDS and in no
lock but the row's own, with `synchronous_commit` off: a heard time lost to a crash is harmless (the
next request writes it again), so its commit never waits on the WAL flush. Nothing is written
between, and the guarded UPDATE that finds the row fresh takes no transaction id. Both times are
Central's clock.
"""
from __future__ import annotations

from central.installation_models import PlayerReports
from contracts.liveness import HEARD_WRITE_SECONDS


def heard_in(conn, player_id: str, authority_epoch: int, now: float) -> None:
    """Central heard from `player_id` on `authority_epoch` at `now`: written when its row is older
    than HEARD_WRITE_SECONDS, names another epoch, or is missing; otherwise nothing is written."""
    fresh = conn.execute(
        "SELECT 1 FROM player_contacts WHERE player_id=%s AND authority_epoch=%s AND heard_at>%s",
        (player_id, authority_epoch, now - HEARD_WRITE_SECONDS)).fetchone()
    if fresh is not None:
        return
    conn.execute("SET LOCAL synchronous_commit = off")
    conn.execute(
        "INSERT INTO player_contacts(player_id,authority_epoch,created_at,heard_at) "
        "VALUES(%s,%s,%s,%s) ON CONFLICT(player_id) DO UPDATE SET "
        "authority_epoch=EXCLUDED.authority_epoch,heard_at=EXCLUDED.heard_at",
        (player_id, authority_epoch, now, now))


def player_reports_in(conn, read_at: float) -> PlayerReports:
    """When Central last heard from each non-retired Player on its current authority epoch,
    labelled with its read time; from the caller's transaction, taking no lock."""
    rows = conn.execute(
        "SELECT c.player_id, c.heard_at FROM players p JOIN player_contacts c "
        "ON c.player_id=p.id AND c.authority_epoch=p.authority_epoch WHERE p.retired_at IS NULL"
    ).fetchall()
    reports = {row["player_id"]: row["heard_at"] for row in rows}
    return PlayerReports(read_at=max((read_at, *reports.values())), reports=reports)


def heard_at_in(conn, player_id: str, authority_epoch: int) -> float | None:
    """When Central last heard from `player_id` on `authority_epoch`; None when never."""
    row = conn.execute("SELECT heard_at FROM player_contacts WHERE player_id=%s "
                       "AND authority_epoch=%s", (player_id, authority_epoch)).fetchone()
    return None if row is None else row["heard_at"]


__all__ = ["heard_at_in", "heard_in", "player_reports_in"]
