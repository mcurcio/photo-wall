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
    def request_power_test(self, frame_id: str) -> PowerTestAccepted:
        """Test: turn off. Replace the Frame's console test with an off of TEST_SECONDS, in one
        transaction that also wakes the worker's Output document source.
        DisplayRefused("unknown_frame", 404), ("frame_unbound", 409)."""
        ...

    def end_power_test(self, frame_id: str) -> None:
        """Turn on: end the Frame's console test, so the request underneath takes over (owner q1,
        2026-10-10; central/displays/model.py, The Output document). Nothing is put on top; with no
        test there is nothing to end. Wakes the source when it ended one. DisplayRefused("unknown_frame", 404)."""
        ...

    def set_power_settings(self, display_id: UUID, settings: DisplayPowerSettings) -> DisplayPowerSettings:
        """Store #116-#118 on the Display and wake the source; DisplayRefused("unknown_display", 404)."""
        ...
