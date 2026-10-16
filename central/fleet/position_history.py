"""A Frame's Position history, pure (configuration snapshots; owner, 2026-10-13: "configuration
snapshots" that keep "a history of what has been done in the past", scope "Everything per
Display+Frame"; migration 096): the Frame fit (mode, corners, rotation) and the Frame's lighting
(decision 0028: its lights, mat depth and paper; migration 099 replaced the mat).

One numbering per Frame, in commit order under the Frame's row lock, for two kinds of entry, each
written where its state converges and never by an editor:

- **Kept** entries: Registry's two writes of the Frame's Position (central/registry.py: Save,
  `commit_calibration_trial_in`; the profile change, `replace_frame_profile`). A kept write that
  finds the stored values differ from the Frame's newest kept entry first appends an unrecorded one
  (`unrecorded_record`, design rule 3).
- **Sent** entries: what the trial owner handed the Node (central/fleet/node_calibration.py
  `deliver_in`), appended only when the handed values differ from the Frame's newest entry
  (`sent_record`): a trial's draft, or the kept values back once a draft went.

Each entry says what it was made for (`MadeFor`: the profile its corners are in and the Display seen
on the Frame's port), which is what Restore checks (`restore_refusal`): a Position made for another
profile or another Display is refused, never adapted. An entry is a record of what was, never a
second source of current state (frames.calibration and frames.lighting stay the truth). Every `at` is
PostgreSQL's clock at the append.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from central.room_light.policy import upgrade_lighting
from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration, CornersFit, CoverFit
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT


class PositionEntryKind(StrEnum):
    """Why an entry was written (the stored `kind`)."""
    KEPT_SAVED = "kept-saved"                       # Done (Save) kept a trial's draft
    KEPT_PROFILE_CHANGED = "kept-profile-changed"   # the Frame's profile changed; its fit and lighting are as they were
    KEPT_UNRECORDED = "kept-unrecorded"             # the stored values differed from the newest kept entry when a
                                                    # kept write came: changed outside Photo Wall before `at`
    SENT_DRAFT = "sent-draft"                       # a trial's draft handed to the Node: not kept
    SENT_BACK = "sent-back"                         # the kept values handed back once a draft went; nothing kept


KEPT_KINDS: Final = frozenset({PositionEntryKind.KEPT_SAVED, PositionEntryKind.KEPT_PROFILE_CHANGED,
                               PositionEntryKind.KEPT_UNRECORDED})
SENT_KINDS: Final = frozenset({PositionEntryKind.SENT_DRAFT, PositionEntryKind.SENT_BACK})


@dataclass(frozen=True)
class PositionValues:
    """What a Position entry records: the Frame fit and the Frame's lighting (the calibration's
    revision is not a value: it only orders saves)."""
    fit: CornersFit | CoverFit
    lighting: FrameLighting


# A new Frame's: the cover fit, and the lighting an upgraded Frame with no mat depth has (one wash of
# the fixed room light "room" at the placeholder Display's matched lux, paper matching the panel), so a
# new Frame shows what an upgraded one shows until its lighting is set.
DEFAULT_POSITION: Final = PositionValues(CoverFit(), upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 0.0))


@dataclass(frozen=True)
class MadeFor:
    """What an entry was made for: the Frame's profile (`profile_px`, width and height in pixels: its
    corners are fractions of that output) and the Display seen on the Frame's bound port then (None:
    unbound, or none seen)."""
    profile_px: tuple[int, int]
    display_id: UUID | None


@dataclass(frozen=True)
class PositionRecord:
    """What a writer appends; the store gives it its id, number, time and `MadeFor`. `session_id`:
    the trial session (stays the same across renewals); `restored_from`: the entry the trial's draft
    was last restored from."""
    kind: PositionEntryKind
    values: PositionValues
    session_id: UUID | None
    restored_from: UUID | None


@dataclass(frozen=True)
class PositionEntry:
    """One stored entry: `number` the Frame's commit order, `at` aware UTC (PostgreSQL's
    clock_timestamp() at the append)."""
    entry_id: UUID
    frame_id: str
    number: int
    at: datetime
    made_for: MadeFor
    record: PositionRecord


@dataclass(frozen=True)
class TrialHanded:
    """What `deliver_in` hands the Node for the Frame's active trial: its draft's values, its session
    and the entry its draft was last restored from."""
    values: PositionValues
    session_id: UUID
    restored_from: UUID | None


class PositionRestoreRefusal(StrEnum):
    """Why Restore refuses an entry (the routes answer each as 409)."""
    OTHER_FRAME = "other_frame"                         # another Frame's entry
    MADE_FOR_OTHER_PROFILE = "made_for_other_profile"   # its corners are in another profile's output
    MADE_FOR_OTHER_DISPLAY = "made_for_other_display"   # made against another Display than the one seen now


def position_values(calibration: Calibration, lighting: FrameLighting) -> PositionValues:
    """The values a stored or drafted Position records: its fit and its lighting."""
    return PositionValues(calibration.fit, lighting)


def unrecorded_record(latest_kept: PositionEntry | None, stored: PositionValues) -> PositionRecord | None:
    """Design rule 3, before a kept write: a KEPT_UNRECORDED record of `stored` (no session, no
    restore) when it differs from the newest kept entry's values, or when the Frame has no kept entry
    and `stored` is not DEFAULT_POSITION; else None."""
    known = DEFAULT_POSITION if latest_kept is None else latest_kept.record.values
    if stored == known:
        return None
    return PositionRecord(PositionEntryKind.KEPT_UNRECORDED, stored, None, None)


def saved_record(values: PositionValues, *, session_id: UUID | None, restored_from: UUID | None) -> PositionRecord:
    """The KEPT_SAVED record of a Save: the trial's draft values, its session and restore."""
    return PositionRecord(PositionEntryKind.KEPT_SAVED, values, session_id, restored_from)


def profile_changed_record(values: PositionValues) -> PositionRecord:
    """The KEPT_PROFILE_CHANGED record of a profile change: the Frame's fit and lighting, unchanged."""
    return PositionRecord(PositionEntryKind.KEPT_PROFILE_CHANGED, values, None, None)


def sent_record(*, trial: TrialHanded | None, saved: PositionValues,
                latest: PositionEntry | None) -> PositionRecord | None:
    """What an exchange appends, from what it hands the Node and the Frame's newest entry (`latest`,
    any kind):
    - an active trial: SENT_DRAFT of its values (its session and restore) when `latest` is None or
      its values differ; else None;
    - no trial: SENT_BACK of `saved` (with `latest`'s session) when `latest` is SENT_DRAFT and its
      values differ from `saved`; else None (the Node already shows what is kept, or nothing was
      ever drafted)."""
    if trial is not None:
        if latest is not None and latest.record.values == trial.values:
            return None
        return PositionRecord(PositionEntryKind.SENT_DRAFT, trial.values, trial.session_id, trial.restored_from)
    if latest is None or latest.record.kind is not PositionEntryKind.SENT_DRAFT or latest.record.values == saved:
        return None
    return PositionRecord(PositionEntryKind.SENT_BACK, saved, latest.record.session_id, None)


def restore_refusal(entry: PositionEntry, *, frame_id: str, profile_px: tuple[int, int],
                    seen_display_id: UUID | None) -> PositionRestoreRefusal | None:
    """Whether Restore refuses `entry` for Frame `frame_id` whose profile is `profile_px` and on whose
    port `seen_display_id` is seen now, in this order: OTHER_FRAME; MADE_FOR_OTHER_PROFILE when its
    `made_for.profile_px` differs; MADE_FOR_OTHER_DISPLAY when both its `made_for.display_id` and
    `seen_display_id` are known and differ. None: it may be restored."""
    if entry.frame_id != frame_id:
        return PositionRestoreRefusal.OTHER_FRAME
    if tuple(entry.made_for.profile_px) != tuple(profile_px):
        return PositionRestoreRefusal.MADE_FOR_OTHER_PROFILE
    made_on = entry.made_for.display_id
    if made_on is not None and seen_display_id is not None and made_on != seen_display_id:
        return PositionRestoreRefusal.MADE_FOR_OTHER_DISPLAY
    return None


def restored_draft(entry: PositionEntry, *, base_revision: int) -> tuple[Calibration, FrameLighting]:
    """The trial draft a Restore loads: `entry`'s fit at `base_revision` (the trial's base, so a save
    made elsewhere is still refused) and its lighting."""
    return Calibration(revision=base_revision, fit=entry.record.values.fit), entry.record.values.lighting


__all__ = ["DEFAULT_POSITION", "KEPT_KINDS", "SENT_KINDS", "MadeFor", "PositionEntry", "PositionEntryKind",
           "PositionRecord", "PositionRestoreRefusal", "PositionValues", "TrialHanded", "position_values",
           "profile_changed_record", "restore_refusal", "restored_draft", "saved_record", "sent_record",
           "unrecorded_record"]
