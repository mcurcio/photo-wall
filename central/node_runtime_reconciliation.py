"""The old evidence path's reconciliation work, finished without effect (decision 0034, S4b part 2).

An Output's loss is judged from the Node's `display` line alone (`OutputReportJudge`,
`central.coordination.OutputLossLedger`): a disconnected Output, never a Player's exit or a surface
invalidated on the old evidence path. Each queued work item therefore finishes as `display_line`,
writing nothing else and taking no advisory lock. S4c deletes this module with the scheduler's
"reconcile" step and the evidence path.
"""
from __future__ import annotations

from central.fleet.node_sessions import NodeSessions


class NodeRuntimeReconciler:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def advance(self, *, limit: int = 32) -> int:
        """Finish up to `limit` due work items as `display_line`; the number finished."""
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("node_reconciliation_limit")
        with self.sessions.db.transaction() as conn:
            now = self.sessions.clock.utc()
            return len(conn.execute(
                "UPDATE node_reconciliation_work SET completed_at=%s,result='display_line' "
                "WHERE work_id IN (SELECT work_id FROM node_reconciliation_work WHERE completed_at IS NULL "
                "AND next_attempt_at<=%s ORDER BY next_attempt_at,work_id LIMIT %s) AND completed_at IS NULL "
                "RETURNING work_id", (now, now, limit)).fetchall())
