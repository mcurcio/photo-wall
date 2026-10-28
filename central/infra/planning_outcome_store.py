"""PgPlanningOutcome: the planning outcome's one row (migration 111), in the caller's transaction."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from central.actor import Actor
from central.planner import Projection
from central.planning_outcome import PlanningIssue, PlanningOutcomeView, issues_of
from contracts.time import Clock

_BY: Actor = "scheduler"


class PgPlanningOutcome:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def record_in(self, conn, projection: Projection) -> None:
        issues = [issue.model_dump(mode="json") for issue in issues_of(projection)]
        dropped = projection.diagnostics_dropped
        row = conn.execute("SELECT issues, dropped FROM planning_outcome WHERE one").fetchone()
        if row is not None and (row["issues"], row["dropped"]) == (issues, dropped):
            return
        conn.execute(
            "INSERT INTO planning_outcome(one, issues, dropped, since, created_at, created_by, "
            "changed_at, changed_by) VALUES (TRUE, %(issues)s, %(dropped)s, to_timestamp(%(now)s), "
            "to_timestamp(%(now)s), %(by)s, to_timestamp(%(now)s), %(by)s) "
            "ON CONFLICT (one) DO UPDATE SET issues=EXCLUDED.issues, dropped=EXCLUDED.dropped, "
            "since=EXCLUDED.since, changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by",
            {"issues": Jsonb(issues), "dropped": dropped, "now": self.clock.utc(), "by": _BY})

    @staticmethod
    def read_in(conn) -> PlanningOutcomeView:
        row = conn.execute("SELECT issues, dropped, EXTRACT(EPOCH FROM since)::float8 AS since "
                           "FROM planning_outcome WHERE one").fetchone()
        if row is None:
            return PlanningOutcomeView()
        return PlanningOutcomeView(
            issues=tuple(PlanningIssue.model_validate(item) for item in row["issues"]),
            since=row["since"], dropped=row["dropped"])
