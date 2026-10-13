"""A Display's picture history, pure (configuration snapshots; owner, 2026-10-13: "configuration
snapshots" that keep "a history of what has been done in the past"; "View and restore"): the
history's records (central/displays/picture_history.py), Restore's rule (central/displays/picture.py
`restored`), the page and time a route reads (central/tuning_history/bounds.py) and an entry's JSON.

The store, the routes and the Done/Copy writes are tests/test_picture_history_store.py's.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from central.displays.lut import LUT_RECIPE_ID
from central.displays.picture import (
    DisplayPicture,
    PictureRefused,
    PictureSession,
    SoftwareChoice,
    SoftwareSetting,
    default_picture,
    restore_of,
    restored,
    reverted,
)
from central.displays.picture_history import (
    PictureEntry,
    PictureEntryKind,
    PictureRecord,
    Port,
    copied_record,
    done_record,
    same_values,
    sent_record,
    unrecorded_record,
)
from central.displays.views import picture_entry_view
from central.live_session import Lease
from central.tuning_history.bounds import (
    PAGE_LIMIT_DEFAULT,
    HistoryRefused,
    history_page,
    utc_instant,
)
from contracts.lut import LutRef
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    HardwareRange,
    HardwareSetting,
    RangeKind,
    SignalRange,
    TestPatternAsk,
)
from contracts.tuning import TestPattern

PROFILE_A, PROFILE_B = "a" * 64, "b" * 64
OLD_RECIPE = "lut33-old-recipe"
OFFERED = (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 60),
           HardwareSetting("12", HardwareRange(RangeKind.LEVEL, 100, ()), 50))
DISPLAY = uuid4()


def _picture(hardware: dict | None = None, *, lut: LutRef | None = None, signal_range: SignalRange = SignalRange.AUTO,
             **software) -> DisplayPicture:
    return DisplayPicture(hardware or {}, replace(NEUTRAL_SOFTWARE, lut=lut, **software), signal_range)


def _session(draft: DisplayPicture | None = None, *, idle_until: float = 10.0) -> PictureSession:
    return PictureSession(uuid4(), DISPLAY, 3, draft or default_picture(), 0, {}, frozenset(), frozenset(),
                          Lease(idle_until, None), None, None)


def _entry(picture: DisplayPicture, kind: PictureEntryKind = PictureEntryKind.KEPT_DONE) -> PictureEntry:
    return PictureEntry(uuid4(), DISPLAY, 1, datetime(2026, 10, 13, 21, 4, 7, tzinfo=UTC), None, None,
                        PictureRecord(kind, picture, None, None, None))


def _restore(session: PictureSession, picture: DisplayPicture, *, offered=OFFERED, lut_listed: bool = True,
             display=DISPLAY, expected: int = 3, now: float = 1.0):
    return restored(session, entry_id=ENTRY, entry_display_id=display, picture=picture, offered=offered,
                    lut_listed=lut_listed, expected_sequence=expected, now=now)


ENTRY = uuid4()


# --- The records -----------------------------------------------------------------------------------

def test_same_values_compares_a_luts_profile_never_its_recipe():
    """Acceptance 4: two pictures differing only in their LUT's recipe set the same picture; another
    profile, a hardware value, a software value or the signal range is a change. Mutation probe
    P1-recipe-compared: compare the LutRef whole -> the recipe pair reads as a change."""
    today = _picture(lut=LutRef(PROFILE_A, LUT_RECIPE_ID))
    assert same_values(today, _picture(lut=LutRef(PROFILE_A, OLD_RECIPE)))
    assert not same_values(today, _picture(lut=LutRef(PROFILE_B, LUT_RECIPE_ID)))
    assert not same_values(today, _picture())
    assert not same_values(today, replace(today, hardware={"10": 46}))
    assert not same_values(today, replace(today, software=replace(today.software, gamma_tenths=24)))
    assert not same_values(today, replace(today, signal_range=SignalRange.FULL))


def test_an_unrecorded_record_only_for_values_no_kept_entry_says():
    """Design rule 3. Mutation probes: P1-no-unrecorded (always None) -> the changed value has no
    record; P1-default-unrecorded (drop the default exception) -> a new Display gets one."""
    assert unrecorded_record(None, default_picture()) is None
    changed = _picture(brightness_percent=120)
    record = unrecorded_record(None, changed)
    assert record == PictureRecord(PictureEntryKind.KEPT_UNRECORDED, changed, None, None, None)
    assert unrecorded_record(_entry(changed), changed) is None
    assert unrecorded_record(_entry(changed), default_picture()) is not None     # back to defaults unseen
    with_lut = _picture(lut=LutRef(PROFILE_A, OLD_RECIPE))
    assert unrecorded_record(_entry(with_lut), replace(with_lut, software=replace(
        with_lut.software, lut=LutRef(PROFILE_A, LUT_RECIPE_ID)))) is None     # a new recipe is no change


def test_done_and_copy_records():
    session = replace(_session(_picture({"10": 46})), restored_from=ENTRY)
    assert done_record(session) == PictureRecord(PictureEntryKind.KEPT_DONE, session.draft, session.session_id,
                                                 ENTRY, None)
    assert copied_record(session.draft) == PictureRecord(PictureEntryKind.KEPT_COPIED, session.draft, None, None,
                                                         None)


# --- Restore ---------------------------------------------------------------------------------------

def test_restore_loads_the_entry_into_the_draft_and_touches_what_changed():
    """Acceptance 5 (pure): the draft becomes the entry's three groups, the sequence rises by one,
    `restored_from` names the entry, and every changed code and setting is touched. Mutation probe
    P1-no-restored-from: leave `restored_from` -> None."""
    session = _session(_picture({"12": 50}, contrast_percent=90, lut=LutRef(PROFILE_A, LUT_RECIPE_ID)))
    entry = _picture({"10": 46}, brightness_percent=114, contrast_percent=90, signal_range=SignalRange.FULL)
    result = _restore(session, entry, now=8.0)
    assert result.session.draft == entry
    assert (result.session.sequence, result.session.restored_from) == (4, ENTRY)
    assert result.session.touched_hardware == {"10", "12"}
    assert result.session.touched_software == {SoftwareSetting.BRIGHTNESS, SoftwareChoice.LUT}
    assert result.session.lease.idle_until > session.lease.idle_until
    assert (result.hardware_skipped, result.lut_skipped, result.lut_recipe_changed) == ((), False, False)


def test_restore_refusals_in_order():
    """Acceptance 6 (pure): session_ended, then stale_sequence, then other_display. Mutation probe
    P1-any-display: drop the display check -> another Display's entry is taken."""
    entry = _picture(brightness_percent=80)
    for session, kwargs, code, status in (
            (_session(idle_until=0.5), {"display": uuid4(), "expected": 2}, "session_ended", 410),
            (_session(), {"display": uuid4(), "expected": 2}, "stale_sequence", 409),
            (_session(), {"display": uuid4()}, "other_display", 409)):
        with pytest.raises(PictureRefused) as refused:
            _restore(session, entry, **kwargs)
        assert (refused.value.code, refused.value.status) == (code, status)


def test_restore_skips_what_the_display_no_longer_offers_and_an_unlisted_lut():
    """Acceptance 7 (pure): a code not offered now is skipped (not in the draft), in picture-list
    order; an unlisted LUT keeps the draft's; the test pattern is the session's, unchanged.
    Mutation probe P1-no-skip: keep every entry code -> "16" lands in the draft."""
    own = LutRef(PROFILE_B, LUT_RECIPE_ID)
    session = replace(_session(_picture(lut=own)), test_pattern=None, display_id=DISPLAY)
    entry = _picture({"16": 3, "10": 46, "14": 5}, lut=LutRef(PROFILE_A, LUT_RECIPE_ID))
    result = _restore(session, entry, lut_listed=False)
    assert dict(result.session.draft.hardware) == {"10": 46}
    assert result.hardware_skipped == ("14", "16")
    assert (result.lut_skipped, result.lut_recipe_changed) == (True, False)
    assert result.session.draft.software.lut == own and SoftwareChoice.LUT not in result.session.touched_software
    unknown = _restore(session, entry, offered=None)
    assert (dict(unknown.session.draft.hardware), unknown.hardware_skipped) == ({}, ("10", "14", "16"))


def test_restore_renders_an_older_recipes_lut_with_todays():
    """Acceptance 7 (pure): an entry LUT baked under another recipe comes back under LUT_RECIPE_ID.
    Mutation probe P1-lut-recipe-kept: keep the entry's LutRef -> the draft names the old recipe."""
    result = _restore(_session(), _picture(lut=LutRef(PROFILE_A, OLD_RECIPE)))
    assert result.session.draft.software.lut == LutRef(PROFILE_A, LUT_RECIPE_ID)
    assert (result.lut_skipped, result.lut_recipe_changed) == (False, True)
    no_lut = _restore(_session(_picture(lut=LutRef(PROFILE_A, LUT_RECIPE_ID))), _picture())
    assert no_lut.session.draft.software.lut is None and not no_lut.lut_recipe_changed


def test_a_code_restore_changed_is_put_back_by_revert():
    """Acceptance 8: a hardware code Restore set where the stored values set none is touched, so
    the ended session's `restore_of` names it at the Pi's first before. Mutation probe
    P1-untouched: leave the touched sets -> nothing is put back."""
    result = _restore(_session(), _picture({"10": 46}))
    ended = replace(result.session, hardware_before={"10": 60})
    assert restore_of(default_picture(), ended) == {"10": 60}
    assert reverted(default_picture(), ended).hardware == {"10": 60}


# --- The page and Central's time --------------------------------------------------------------------

def test_utc_instant_takes_a_zone_and_refuses_a_naive_time():
    """Acceptance 9 (pure). Mutation probe P1-naive-time: read a naive time as UTC -> taken."""
    assert utc_instant("2026-10-13T21:04:07Z") == datetime(2026, 10, 13, 21, 4, 7, tzinfo=UTC)
    assert utc_instant("2026-10-13T22:04:07+01:00") == datetime(2026, 10, 13, 21, 4, 7, tzinfo=UTC)
    for text in ("2026-10-13T21:04:07", "yesterday", ""):
        with pytest.raises(HistoryRefused) as refused:
            utc_instant(text)
        assert (refused.value.code, refused.value.status) == ("bad_time", 422)


def test_history_page_bounds():
    assert history_page(None, None).limit == PAGE_LIMIT_DEFAULT and history_page(None, None).before is None
    assert history_page("2026-10-13T21:04:07Z", 500).limit == 500
    for limit in (0, 501, -1):
        with pytest.raises(HistoryRefused) as refused:
            history_page(None, limit)
        assert (refused.value.code, refused.value.status) == ("bad_range", 422)


def test_an_entrys_json_names_utc_times_and_todays_names():
    """Acceptance 12 (pure). Mutation probe P1-naive-json: a naive `at` is refused by the view."""
    at = datetime(2026, 10, 13, 21, 4, 7, 250_000, tzinfo=UTC)
    entry = PictureEntry(uuid4(), DISPLAY, 7, at, Port("pi", "HDMI-A-1"), "kitchen",
                         PictureRecord(PictureEntryKind.SENT_DRAFT, _picture({"10": 46}, lut=LutRef(PROFILE_A, OLD_RECIPE)),
                                       uuid4(), None, TestPattern.WHITE))
    body = json.loads(picture_entry_view(entry, display_name="Samsung", frame_name="Kitchen",
                                         lut_name=None).model_dump_json())
    assert datetime.fromisoformat(body["at"]).utcoffset() == timedelta(0)
    assert body["frame"] == {"id": "kitchen", "name": "Kitchen"} and body["test_pattern"] == "white"
    assert body["picture"]["lut_recipe_id"] == OLD_RECIPE
    assert body["picture"]["software"]["lut"] == {"profile_sha256": PROFILE_A, "name": "Paper profile aaaaaaaa"}
    for naive in (at.replace(tzinfo=None), at.astimezone(timezone(timedelta(hours=2))).replace(tzinfo=None)):
        with pytest.raises(ValueError):
            picture_entry_view(replace(entry, at=naive), display_name="Samsung", frame_name=None, lut_name=None)


# --- Sent entries (slice S2) ------------------------------------------------------------------------

def _sent(kind: PictureEntryKind, picture: DisplayPicture, session_id=None) -> PictureEntry:
    return PictureEntry(uuid4(), DISPLAY, 4, datetime(2026, 10, 13, 21, 4, 7, tzinfo=UTC), Port("pi", "HDMI-A-1"),
                        "kitchen", PictureRecord(kind, picture, session_id, None, None))


def test_sent_record_while_a_session_is_live_is_its_draft_with_its_pattern():
    """Acceptance 10, branch 1. Mutation probe P2-kind-from-stored is the DB test's."""
    session = replace(_session(_picture(brightness_percent=90)), restored_from=ENTRY)
    ask = TestPatternAsk(TestPattern.GREY_RAMP, 4)
    record = sent_record(session=session, now=1.0, previous=None, picture=session.draft, test_pattern=ask, display_id=DISPLAY)
    assert record == PictureRecord(PictureEntryKind.SENT_DRAFT, session.draft, session.session_id, ENTRY,
                                   TestPattern.GREY_RAMP)


def test_sent_record_after_a_draft_went_is_back_only_when_the_values_differ():
    """Acceptance 10, branches 2 and 3: a lapsed session is no session. Mutation probes:
    P2-always-kept (SENT_KEPT with no live session) -> no SENT_BACK; P2-lease-ignored (any present
    session is live) -> the lapsed one still reads SENT_DRAFT."""
    draft_id = uuid4()
    previous = _sent(PictureEntryKind.SENT_DRAFT, _picture(brightness_percent=90), draft_id)
    lapsed = _session(_picture(brightness_percent=90), idle_until=0.5)
    back = sent_record(session=lapsed, now=1.0, previous=previous, picture=default_picture(), test_pattern=None, display_id=DISPLAY)
    assert back == PictureRecord(PictureEntryKind.SENT_BACK, default_picture(), draft_id, None, None)
    same = sent_record(session=None, now=1.0, previous=previous, picture=_picture(brightness_percent=90),
                       test_pattern=None, display_id=DISPLAY)
    assert same == PictureRecord(PictureEntryKind.SENT_KEPT, _picture(brightness_percent=90), None, None, None)


def test_sent_record_with_no_draft_before_is_the_kept_values():
    """Acceptance 10, branch 4: no entry yet (a Display newly seen), or the previous one not a draft."""
    for previous in (None, _sent(PictureEntryKind.SENT_KEPT, _picture(brightness_percent=90)),
                     _sent(PictureEntryKind.SENT_BACK, _picture(brightness_percent=90), uuid4())):
        record = sent_record(session=None, now=1.0, previous=previous, picture=default_picture(), test_pattern=None, display_id=DISPLAY)
        assert record == PictureRecord(PictureEntryKind.SENT_KEPT, default_picture(), None, None, None)


def test_sent_record_refuses_a_test_pattern_with_no_live_session():
    """Mutation probe P2-pattern-allowed: drop the ValueError -> a pattern is recorded as kept."""
    ask = TestPatternAsk(TestPattern.WHITE, 2)
    for session in (None, _session(idle_until=0.5)):
        with pytest.raises(ValueError, match="sent_test_pattern"):
            sent_record(session=session, now=1.0, previous=None, picture=default_picture(), test_pattern=ask, display_id=DISPLAY)


def test_sent_record_after_another_displays_draft_is_the_kept_values():
    """A panel swap: the port's newest sent entry is Display A's draft, and the port now reports
    Display B. Nothing of B's was put back, so B's first entry is SENT_KEPT, never SENT_BACK naming
    A's session."""
    previous = _sent(PictureEntryKind.SENT_DRAFT, _picture(brightness_percent=90), uuid4())
    record = sent_record(session=None, now=1.0, previous=previous, picture=default_picture(), test_pattern=None,
                         display_id=uuid4())
    assert record == PictureRecord(PictureEntryKind.SENT_KEPT, default_picture(), None, None, None)
