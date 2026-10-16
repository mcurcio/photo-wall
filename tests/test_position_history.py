"""A Frame's Position history, pure (configuration snapshots, slice S3; owner, 2026-10-13: scope
"Everything per Display+Frame", use "View and restore"): central/fleet/position_history.py's records,
what an exchange appends, and Restore's refusals and draft.

The store, Registry's kept writes, the exchanges and the routes are tests/test_position_history_store.py's.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from support.position import LIGHTING

from central.fleet.position_history import (
    DEFAULT_POSITION,
    MadeFor,
    PositionEntry,
    PositionEntryKind,
    PositionRecord,
    PositionRestoreRefusal,
    PositionValues,
    TrialHanded,
    position_values,
    profile_changed_record,
    restore_refusal,
    restored_draft,
    saved_record,
    sent_record,
    unrecorded_record,
)
from contracts.frame_lighting import FarLight, FrameLighting, Paper
from contracts.models import Calibration, CornersFit, CoverFit

SKEWED = PositionValues(CornersFit(corners=((0.1, 0), (1, 0), (1, 1), (0, 1))), LIGHTING)
MATTED = PositionValues(CoverFit(), FrameLighting(
    mat_depth_mm=2.0, lights=(FarLight(light="room", lux=400.0, reference={"kind": "at-full"}, from_degrees=90,
                                       steep_degrees=30, softness_degrees=5),),
    paper=Paper(whiteness=0.9, ink_density=2.3)))
PORTRAIT, LANDSCAPE = (1080, 1920), (1920, 1080)
SAMSUNG, DELL = uuid4(), uuid4()
SESSION = uuid4()


def _entry(kind: PositionEntryKind, values: PositionValues, *, frame_id: str = "kitchen", profile=PORTRAIT,
           display=SAMSUNG, session_id=None) -> PositionEntry:
    return PositionEntry(uuid4(), frame_id, 3, datetime(2026, 10, 13, 21, 4, 7, tzinfo=UTC),
                         MadeFor(profile, display), PositionRecord(kind, values, session_id, None))


def test_position_values_and_kept_records():
    calibration = Calibration(revision=4, fit=SKEWED.fit)
    assert position_values(calibration, LIGHTING) == SKEWED
    restored = uuid4()
    assert saved_record(SKEWED, session_id=SESSION, restored_from=restored) == PositionRecord(
        PositionEntryKind.KEPT_SAVED, SKEWED, SESSION, restored)
    assert profile_changed_record(MATTED) == PositionRecord(PositionEntryKind.KEPT_PROFILE_CHANGED, MATTED, None, None)


def test_an_unrecorded_record_only_for_values_no_kept_entry_says():
    """Design rule 3. Mutation probes: P3-no-unrecorded (always None) -> no record of the SQL lighting;
    P3-default-unrecorded (no default exception) -> a new Frame at its defaults gets one."""
    assert unrecorded_record(None, DEFAULT_POSITION) is None
    assert unrecorded_record(None, MATTED) == PositionRecord(PositionEntryKind.KEPT_UNRECORDED, MATTED, None, None)
    assert unrecorded_record(_entry(PositionEntryKind.KEPT_SAVED, MATTED), MATTED) is None
    assert unrecorded_record(_entry(PositionEntryKind.KEPT_SAVED, SKEWED), MATTED) is not None


def test_sent_record_with_a_trial_appends_its_draft_only_when_it_changed():
    """Acceptance 11, branches 1 and 2. Mutation probe P3-every-exchange: append unconditionally ->
    the unchanged draft appends again."""
    restored = uuid4()
    handed = TrialHanded(SKEWED, SESSION, restored)
    assert sent_record(trial=handed, saved=DEFAULT_POSITION, latest=None) == PositionRecord(
        PositionEntryKind.SENT_DRAFT, SKEWED, SESSION, restored)
    assert sent_record(trial=handed, saved=DEFAULT_POSITION,
                       latest=_entry(PositionEntryKind.KEPT_SAVED, DEFAULT_POSITION)) is not None
    assert sent_record(trial=handed, saved=DEFAULT_POSITION,
                       latest=_entry(PositionEntryKind.SENT_DRAFT, SKEWED, session_id=SESSION)) is None
    assert sent_record(trial=handed, saved=SKEWED, latest=_entry(PositionEntryKind.KEPT_SAVED, SKEWED)) is None


def test_sent_record_with_no_trial_is_back_only_after_a_differing_draft():
    """Acceptance 11, branches 3 and 4."""
    draft = _entry(PositionEntryKind.SENT_DRAFT, SKEWED, session_id=SESSION)
    assert sent_record(trial=None, saved=DEFAULT_POSITION, latest=draft) == PositionRecord(
        PositionEntryKind.SENT_BACK, DEFAULT_POSITION, SESSION, None)
    assert sent_record(trial=None, saved=SKEWED, latest=draft) is None                 # saved as drafted
    assert sent_record(trial=None, saved=DEFAULT_POSITION, latest=None) is None
    for kind in (PositionEntryKind.KEPT_SAVED, PositionEntryKind.SENT_BACK):
        assert sent_record(trial=None, saved=DEFAULT_POSITION, latest=_entry(kind, SKEWED)) is None


def test_restore_refusal_order():
    """Acceptance 11: other Frame, then another profile, then another Display (both known). Mutation
    probe P3-refusal-order: test the profile first -> the other Frame's entry reads the profile."""
    everything_else = _entry(PositionEntryKind.KEPT_SAVED, SKEWED, frame_id="hall", profile=LANDSCAPE, display=DELL)
    assert restore_refusal(everything_else, frame_id="kitchen", profile_px=PORTRAIT,
                           seen_display_id=SAMSUNG) is PositionRestoreRefusal.OTHER_FRAME
    other_profile = _entry(PositionEntryKind.KEPT_SAVED, SKEWED, profile=LANDSCAPE, display=DELL)
    assert restore_refusal(other_profile, frame_id="kitchen", profile_px=PORTRAIT,
                           seen_display_id=SAMSUNG) is PositionRestoreRefusal.MADE_FOR_OTHER_PROFILE
    other_display = _entry(PositionEntryKind.KEPT_SAVED, SKEWED, display=DELL)
    assert restore_refusal(other_display, frame_id="kitchen", profile_px=PORTRAIT,
                           seen_display_id=SAMSUNG) is PositionRestoreRefusal.MADE_FOR_OTHER_DISPLAY
    for display, seen in ((None, SAMSUNG), (DELL, None), (SAMSUNG, SAMSUNG)):
        entry = _entry(PositionEntryKind.KEPT_SAVED, SKEWED, display=display)
        assert restore_refusal(entry, frame_id="kitchen", profile_px=PORTRAIT, seen_display_id=seen) is None


def test_a_restored_draft_keeps_the_trials_base_revision():
    """Mutation probe P3-entry-revision is the DB test's (an entry has no revision of its own)."""
    calibration, lighting = restored_draft(_entry(PositionEntryKind.SENT_DRAFT, MATTED), base_revision=7)
    assert calibration == Calibration(revision=7, fit=MATTED.fit) and lighting == MATTED.lighting
