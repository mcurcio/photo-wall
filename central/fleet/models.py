"""Fleet domain values: the exact bytes an offer names, and the fleet's HTTP-mapped errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
    """Why a Frame's Position trial (begin, an operation, a Restore) was refused, as the trial routes
    answer it ({"error": code}, raised as NodeControlError(code, status)). A Restore's entry refusals
    are position_history.PositionRestoreRefusal and a draft lighting's FrameLightingRefusal."""
    ALREADY_ACTIVE = "trial_already_active"                         # 409: another session edits the Frame
    BASELINE_REVISION_CHANGED = "trial_baseline_revision_changed"   # 409: saved elsewhere since the draft
    UNKNOWN = "trial_unknown"                                       # 404
    ENDED = "trial_ended"                                           # 410: a Restore into a trial that ended
    SEQUENCE_CONFLICT = "trial_sequence_conflict"                   # 409: another window's edit came first
    SEQUENCE_INVALID = "trial_sequence_invalid"                     # 422
    LATEST_NOT_PRESENTED = "trial_latest_not_presented"             # 409: Save before the Pi showed the draft
    FRAME_UNBOUND = "trial_frame_unbound"                           # 409: no Pi output feeds the Frame
    DISPLAY_UNAVAILABLE = "trial_display_unavailable"               # 409: the Pi's display host is away
    CURRENT_OUTPUT_REQUIRED = "trial_current_output_required"       # 409: the output was not reported lately
    ADMITTED_SURFACE_REQUIRED = "trial_admitted_surface_required"   # 409: the Pi is still starting its picture
    UNKNOWN_ENTRY = "unknown_entry"                                 # 404: a Restore's entry is gone
    OPERATION_INVALID = "node_trial_operation_invalid"              # 422: a body the routes cannot read
    BODY_TOO_LARGE = "node_body_too_large"                          # 413
    CALIBRATION_REVISION_CONFLICT = "calibration_revision_conflict"        # 422: Save, the Registry's check
    CALIBRATION_BASELINE_CHANGED = "calibration_trial_baseline_changed"    # 422: Save, the Registry's check
