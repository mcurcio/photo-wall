"""The operator API's Position history shapes (configuration snapshots; migration 096): a Frame's
Position history and the Position tab's Restore. Plain models, one per answer. Times are aware UTC
(ISO 8601 in JSON), PostgreSQL's clock at the append. The rules are
central/fleet/position_history.py's.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field

from central.fleet.position_history import PositionEntry, PositionEntryKind, PositionRestoreRefusal
from central.installation_models import FrameRefView
from contracts.frame_lighting import FrameLighting
from contracts.models import FrameFit, Model


class ProfilePxView(Model):
    """A Frame profile's output in pixels."""
    width: Annotated[int, Field(ge=1, le=16384)]
    height: Annotated[int, Field(ge=1, le=16384)]


class MadeForView(Model):
    """What an entry was made for: the profile its corners are in and the Display seen on the Frame's
    port then (None: none)."""
    profile_px: ProfilePxView
    display_id: UUID | None


class PositionEntryView(Model):
    """One Position history entry (central/fleet/position_history.py). `restore_refusal`: what Restore
    would answer for it on this Frame now (`position_history.restore_refusal`; None: it may be
    restored), so the tab can say why before anyone presses it."""
    entry_id: UUID
    frame: FrameRefView
    kind: PositionEntryKind
    number: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    session_id: UUID | None
    restored_from: UUID | None
    fit: FrameFit
    lighting: FrameLighting
    made_for: MadeForView
    restore_refusal: PositionRestoreRefusal | None


class PositionHistoryView(Model):
    """GET /v1/operator/frames/{frame_id}/position-history: the Frame's entries, newest first by `at`
    then number. `unrecorded_change`: the Frame's stored fit and lighting differ from its newest kept
    entry (design rule 3, flagged until the next kept write records it)."""
    frame: FrameRefView
    unrecorded_change: bool
    entries: tuple[PositionEntryView, ...]


class PositionRestore(Model):
    """POST /v1/operator/frames/{frame_id}/calibration-trials/{trial_id}/restores: load a Position
    history entry into the active trial's draft (central/fleet/node_calibration.py `restore`)."""
    entry_id: UUID
    expected_sequence: Annotated[int, Field(ge=1)]


def position_entry_view(entry: PositionEntry, *, frame_name: str,
                        restore_refusal: PositionRestoreRefusal | None) -> PositionEntryView:
    """`entry` as the routes answer it, with the Frame's name today and Restore's answer for it now."""
    width, height = entry.made_for.profile_px
    record = entry.record
    return PositionEntryView(
        entry_id=entry.entry_id, frame=FrameRefView(id=entry.frame_id, name=frame_name), kind=record.kind,
        number=entry.number, at=entry.at, session_id=record.session_id, restored_from=record.restored_from,
        fit=record.values.fit, lighting=record.values.lighting,
        made_for=MadeForView(profile_px=ProfilePxView(width=width, height=height),
                             display_id=entry.made_for.display_id),
        restore_refusal=restore_refusal)


__all__ = ["MadeForView", "PositionEntryView", "PositionHistoryView", "PositionRestore", "ProfilePxView",
           "position_entry_view"]
