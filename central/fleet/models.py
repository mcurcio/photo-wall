"""Fleet domain values: the exact bytes an offer names, and the fleet's HTTP-mapped errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


@dataclass(frozen=True, slots=True)
class OfferAsset:
    """Frozen exact bytes a node boot offer or a node app command names."""

    kind: str
    tag: str
    content_key: str
    sha256: str
    size: int
    format: str | None = None


class FleetError(Exception):
    def __init__(self, code: str, status: int = 409, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


class NodeControlError(ValueError):
    """A node or operator fleet request refused: `code`, its HTTP `status`, optional `details`."""

    def __init__(self, code: str, status: int = 409, *, details: dict | None = None):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details or {}


class TrialRefusal(StrEnum):
    """Why a Frame's Position trial (begin, an operation, a Restore) was refused: every code the trial
    routes answer ({"error": code}), each raised as TrialRefused(code) with its status from
    TRIAL_REFUSAL_STATUS. A Restore's entry refusals are position_history.PositionRestoreRefusal and
    a draft lighting's FrameLightingRefusal."""
    ALREADY_ACTIVE = "trial_already_active"                         # another session edits the Frame
    BASELINE_REVISION_CHANGED = "trial_baseline_revision_changed"   # saved elsewhere since the draft
    UNKNOWN = "trial_unknown"
    ENDED = "trial_ended"                                           # a Restore into a trial that ended
    SEQUENCE_CONFLICT = "trial_sequence_conflict"                   # another window's edit came first
    SEQUENCE_INVALID = "trial_sequence_invalid"
    LATEST_NOT_PRESENTED = "trial_latest_not_presented"             # Save before the Pi showed the draft
    FRAME_UNBOUND = "trial_frame_unbound"                           # no Pi output feeds the Frame
    DISPLAY_UNAVAILABLE = "trial_display_unavailable"               # the Pi's display host is away
    CURRENT_OUTPUT_REQUIRED = "trial_current_output_required"       # the output was not reported lately
    ADMITTED_SURFACE_REQUIRED = "trial_admitted_surface_required"   # the Pi is still starting its picture
    SURFACE_CHANGED = "trial_surface_changed"     # the Runtime no longer admits the output or app as read
    UNKNOWN_ENTRY = "unknown_entry"                                 # a Restore's entry is gone
    OPERATION_INVALID = "node_trial_operation_invalid"              # a body the routes cannot read
    BODY_TOO_LARGE = "node_body_too_large"
    CALIBRATION_REVISION_CONFLICT = "calibration_revision_conflict"        # Save, the Registry's check
    CALIBRATION_BASELINE_CHANGED = "calibration_trial_baseline_changed"    # Save, the Registry's check
    POSITION_OFF_SCREEN = "position_off_screen"     # Save: a Rectangle runs off its screen as it hangs


TRIAL_REFUSAL_STATUS: Final[dict[TrialRefusal, int]] = {
    TrialRefusal.ALREADY_ACTIVE: 409, TrialRefusal.BASELINE_REVISION_CHANGED: 409,
    TrialRefusal.UNKNOWN: 404, TrialRefusal.ENDED: 410, TrialRefusal.SEQUENCE_CONFLICT: 409,
    TrialRefusal.SEQUENCE_INVALID: 422, TrialRefusal.LATEST_NOT_PRESENTED: 409,
    TrialRefusal.FRAME_UNBOUND: 409, TrialRefusal.DISPLAY_UNAVAILABLE: 409,
    TrialRefusal.CURRENT_OUTPUT_REQUIRED: 409, TrialRefusal.ADMITTED_SURFACE_REQUIRED: 409,
    TrialRefusal.SURFACE_CHANGED: 409, TrialRefusal.UNKNOWN_ENTRY: 404,
    TrialRefusal.OPERATION_INVALID: 422, TrialRefusal.BODY_TOO_LARGE: 413,
    TrialRefusal.CALIBRATION_REVISION_CONFLICT: 409, TrialRefusal.CALIBRATION_BASELINE_CHANGED: 409,
    TrialRefusal.POSITION_OFF_SCREEN: 422,
}


class TrialRefused(NodeControlError):
    """A trial refusal: `code` from TrialRefusal, answered with its TRIAL_REFUSAL_STATUS."""

    def __init__(self, code: TrialRefusal) -> None:
        code = TrialRefusal(code)
        super().__init__(code, TRIAL_REFUSAL_STATUS[code])
