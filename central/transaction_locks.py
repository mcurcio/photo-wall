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


# Issue #127: the Node's own lock, so a Node's display exchange never waits on the fleet-wide
# Coordination lock (owner, 2026-10-13: "remove fleet-wide lock serialization + per-poll delivery
# churn"). The two-key advisory form: this class and hashtext(device_id), so Nodes never contend.
NODE_LOCK_CLASS = 734118325


def acquire_node_lock(conn, device_id: str) -> None:
    """Take the Node's transaction lock: `pg_advisory_xact_lock(NODE_LOCK_CLASS, hashtext(device_id))`.
    Serializes that Node's display exchanges and admissions, and nothing else. Lock order:
    Coordination, Runtime, then Node (an operator calibration trial, in
    `NodeDisplay.current_frame_in`, and an exchange reporting a completion take the Node lock
    after them); a holder of a Node lock never takes Coordination or Runtime after it, so no
    lock-order cycle exists. A Node's steady exchange takes the Node lock alone."""
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (NODE_LOCK_CLASS, device_id))


def holds_node_lock_in(conn, device_id: str) -> bool:
    """Whether this backend holds `device_id`'s Node lock (pg_locks, advisory, granted, classid =
    NODE_LOCK_CLASS, objid = hashtext(device_id), objsubid = 2): the check
    `Coordinator.display_admission_in` and `display_withdrawal_in` make in place of
    `holds_runtime_locks_in`."""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted "
        "AND pid=pg_backend_pid() AND classid=%s::oid AND objid=hashtext(%s)::oid "
        "AND objsubid=2) AS held",
        (NODE_LOCK_CLASS, device_id),
    ).fetchone()["held"]
