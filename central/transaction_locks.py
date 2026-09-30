"""Shared advisory lock order for current equipment and Runtime state."""

COORDINATION_LOCK = 734118324
RUNTIME_LOCK = 734118323


def acquire_runtime_locks(conn) -> None:
    """Take Coordination before Runtime, including caller-owned transactions."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (RUNTIME_LOCK,))


def holds_runtime_locks_in(conn) -> bool:
    """Check that both transaction-cut locks are held by this backend.

    A caller-owned operation can use this before reading Runtime if it must
    avoid silently taking Coordination/Runtime after later equipment locks.
    """
    held = conn.execute(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' "
        "AND pid=pg_backend_pid() AND granted AND classid=0 AND objsubid=1 "
        "AND objid=ANY(%s::oid[])",
        ([COORDINATION_LOCK, RUNTIME_LOCK],),
    ).fetchone()["n"]
    return held == 2
