"""What the last planning pass could not do, kept for the operator (pure: the model, the rule and
the port; `central/infra/planning_outcome_store.py` keeps it).

A pass never refuses the wall (central/planner.py `project`): an Intent it cannot plan is
`intent_refused`, a budget shortens the plan, and a Frame's photo trouble is a diagnostic. Those
diagnostics, without their per-showing assignment ids, are the planning issues; the console's
Needs attention lists them (a refused Scene names its Scene and Run)."""

from __future__ import annotations

from typing import Protocol

from central.planner import Projection
from contracts.models import Instant, Model


class PlanningIssue(Model):
    code: str
    target: str | None = None
    scene_id: str | None = None
    run_id: str | None = None
    source_ref: str | None = None
    reason: str | None = None


class PlanningOutcomeView(Model):
    """The current issues, since when this same set has stood (None before the first pass
    recorded one), and how many diagnostics the pass counted past its budget."""

    issues: tuple[PlanningIssue, ...] = ()
    since: Instant | None = None
    dropped: int = 0


def _order(issue: PlanningIssue) -> tuple:
    return tuple("" if value is None else value for value in (
        issue.code, issue.scene_id, issue.run_id, issue.target, issue.source_ref, issue.reason))


def issues_of(projection: Projection) -> tuple[PlanningIssue, ...]:
    """A projection's diagnostics as planning issues: one per (code, Scene, Run, target, Source,
    reason), sorted, so the same trouble on every showing of a Frame is one issue and the set is
    the same from one pass to the next while nothing changes."""
    issues = {PlanningIssue(code=item.code, target=item.target, scene_id=item.scene_id,
                            run_id=item.run_id, source_ref=item.source_ref, reason=item.reason)
              for item in projection.diagnostics}
    return tuple(sorted(issues, key=_order))


class PlanningOutcomes(Protocol):
    """The planning outcome's store, called in the caller's transaction."""

    def record_in(self, conn, projection: Projection) -> None:
        """Keep this pass's issues; written only when they differ from the kept ones."""
        ...

    def read_in(self, conn) -> PlanningOutcomeView:
        ...
