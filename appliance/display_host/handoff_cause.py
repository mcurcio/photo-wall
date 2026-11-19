"""Why Display hands a connected Output to no run: the handoff's own vocabulary
(appliance/display_host/handoff.py `not_handed_off` decides it), stated per Output in Display's
feed snapshot (appliance/display_host/runner.py) and read by the health judge, which names the
dark Frame's cause from it (appliance/health/judge.py). Stdlib only, so the judge's closure takes
no compositor code."""
from __future__ import annotations

from enum import StrEnum


class NotHandedOff(StrEnum):
    """Why a connected Output is handed to no run."""

    NO_FRAME = "no_frame"   # its Output document binds no Frame (or none came)
    NO_RUN = "no_run"       # the app broker has told Display no current run


__all__ = ["NotHandedOff"]
