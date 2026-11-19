"""PgPlanningOutcome: the planning outcome as the `singletons` document 'planning_outcome'
(decision 0034's migration 118), in the caller's transaction. Written only when the set of issues
or the dropped count changes, so the row's `changed_at` is when this set began (`since`)."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from central.actor import Actor
from central.db import TransactionClock
from central.infra.singletons import Singleton
from central.planner import Projection
from central.planning_outcome import PlanningIssue, PlanningOutcomeView, issues_of

_BY: Final[Actor] = "scheduler"


class _PlanningOutcomeDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    issues: tuple[PlanningIssue, ...]
    dropped: int


PLANNING_OUTCOME: Final[Singleton[_PlanningOutcomeDocument]] = Singleton(
    "planning_outcome", _PlanningOutcomeDocument)


class PgPlanningOutcome:
    def __init__(self, times: TransactionClock) -> None:
        """`times` stamps the audit times (the database's clock in production, G11)."""
        self.times = times

    def record_in(self, conn, projection: Projection) -> None:
        document = _PlanningOutcomeDocument(issues=issues_of(projection),
                                            dropped=projection.diagnostics_dropped)
        PLANNING_OUTCOME.write_in(conn, document, by=_BY, times=self.times)

    @staticmethod
    def read_in(conn) -> PlanningOutcomeView:
        stored = PLANNING_OUTCOME.read_in(conn)
        if stored is None:
            return PlanningOutcomeView()
        return PlanningOutcomeView(issues=stored.value.issues, since=stored.changed_at,
                                   dropped=stored.value.dropped)
