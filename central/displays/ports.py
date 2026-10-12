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
    FramePictureView,
    FramePowerView,
    HardwareEdit,
    PictureCopyRequest,
    PictureCopyResult,
    PictureSequence,
    PictureSessionView,
    PowerTestAccepted,
    SoftwareEdit,
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


# Roadmap 1c's Live model, two groups from the picture split (decision 0022, slice S2). Refusals are DisplayRefused with
# the codes of central/displays/picture.py PictureRefused, plus unknown_frame 404, frame_unbound
# 409, no_display 409, session_open 409 and unknown_session 404. Every write wakes the worker's
# Output document source in its own transaction, as the power commands do.

class PictureQueries(Protocol):
    def frame_picture(self, frame_id: str) -> FramePictureView:
        """The Picture tab's facts, the open session's draft in force; DisplayRefused("unknown_frame", 404)."""
        ...


class PictureCommands(Protocol):
    def begin_picture(self, frame_id: str) -> PictureSessionView:
        """Open a picture session on the Display last seen on the Frame's bound Output, recording
        the Output's picture change (`began_at_change`). An ended session on that Display is folded
        first (its `reverted` result stored). unknown_frame, frame_unbound, no_display,
        session_open (a live session on that Display)."""
        ...

    def edit_hardware(self, session_id: UUID, edit: HardwareEdit) -> PictureSequence:
        """One display setting's new value, or None to hand it back (as the display has it), live on
        the Display; checked against what the display offers (`hardware.edit_refusal`).
        unknown_session, session_ended, stale_sequence, not_offered, out_of_range."""
        ...

    def edit_software(self, session_id: UUID, edit: SoftwareEdit) -> PictureSequence:
        """One of Photo Wall's four, live on the Display. unknown_session, session_ended,
        stale_sequence, out_of_range."""
        ...

    def keep_picture_alive(self, session_id: UUID) -> PictureSessionView:
        """Slide the session's idle deadline; unknown_session, session_ended."""
        ...

    def picture_done(self, session_id: UUID) -> None:
        """Store the draft on the Display and end the session; not_presented_yet until
        picture_status is SHOWN for the latest change. unknown_session, session_ended."""
        ...

    def revert_picture(self, session_id: UUID) -> None:
        """Store `reverted(...)` and end the session. unknown_session, session_ended."""
        ...

    def copy_picture(self, frame_id: str, request: PictureCopyRequest) -> tuple[PictureCopyResult, ...]:
        """Copy the source Frame's Display's software values, and its hardware values where each
        target offers them (`picture.copied`), to each target Frame's Display, one outcome per target
        in request order, naming the hardware codes skipped; never position. unknown_frame (the
        source, or any target), no_display (the source)."""
        ...

