"""What the operator routes depend on (roadmap 1b; run ledger .claude/runs/display-1b.md, slices
C1-C3): one plain method per question, never a generic apply. central/infra/display_store.py
implements both over PostgreSQL; central/display_routes.py depends only on these.
"""
from __future__ import annotations

from typing import Protocol
from uuid import UUID

from central.displays.views import (
    DisplayPowerSettings,
    FrameDisplayView,
    FramePowerView,
    PowerTestAccepted,
)
from contracts.node_output import Power


class DisplayRefused(Exception):
    """A refusal the routes answer as {"error": code} with `status`: unknown_frame 404,
    unknown_display 404, frame_unbound 409."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class DisplayQueries(Protocol):
    def frame_display(self, frame_id: str) -> FrameDisplayView:
        """The Hardware tab's Display facts; DisplayRefused("unknown_frame", 404)."""
        ...

    def frame_power(self, frame_id: str) -> FramePowerView:
        """The Power tab's facts; DisplayRefused("unknown_frame", 404)."""
        ...


class DisplayCommands(Protocol):
    def request_power_test(self, frame_id: str, power: Power) -> PowerTestAccepted:
        """Replace the Frame's console test with one of TEST_SECONDS for `power` (Turn on ends a
        test-off by putting a test-on on top), in one transaction that also wakes the worker's
        Output document source. DisplayRefused("unknown_frame", 404), ("frame_unbound", 409)."""
        ...

    def set_power_settings(self, display_id: UUID, settings: DisplayPowerSettings) -> DisplayPowerSettings:
        """Store #116-#118 on the Display and wake the source; DisplayRefused("unknown_display", 404)."""
        ...
