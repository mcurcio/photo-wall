"""Typed handoff of execution facts to Runtime and Planning policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import Field

from contracts.models import Identifier, Instant, Model

OutcomeKind = Literal[
    "group_skipped",
    "observation",
    "offer_backpressure",
    "offer_unavailable",
    "readiness_lost",
]


class ExecutionOutcome(Model):
    kind: OutcomeKind
    occurred_at: Instant
    player_id: Identifier | None = None
    assignment_id: Identifier | None = None
    detail: dict = Field(default_factory=dict)


class OutcomeHandling(Model):
    runtime: Literal["preserve_lifecycle", "record_observation"]
    planner: Literal[
        "await_capacity",
        "await_media",
        "preserve_selection",
        "record_observation",
        "replan",
    ]


class ExecutionOutcomeRouter:
    """Deliver every execution fact to both domain owners before auditing it."""

    def __init__(
        self,
        runtime: Callable[[ExecutionOutcome], str],
        planner: Callable[[ExecutionOutcome], str],
    ):
        self.runtime = runtime
        self.planner = planner

    def handle(self, outcome: ExecutionOutcome) -> OutcomeHandling:
        return OutcomeHandling(
            runtime=self.runtime(outcome),
            planner=self.planner(outcome),
        )
