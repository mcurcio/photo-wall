"""A Frame's status rules (decision 0036), pure: `central/frame_status/policy.py` over hand-built
facts. No database, no clock but the `now` handed in.

One test per row of the decision table, in order: the row's own facts give the row, and with the
row above it also true the row above wins (where the two can hold together; rows 5, 6 and 7 split
one condition, the display wanted off, and each says which side it is). Then the cause table,
exhaustive over contracts/node_faults.py and `FrameBlackCause`; every problem's part; "since
when"; the house word; a house problem replacing the Frame problems it explains.

Mutation probes: swap two adjacent rows of `ROWS` (rows 3 and 4, or 11 and 12) -> that row's
precedence test fails; drop a row of `CAUSES` -> the exhaustiveness test fails.
"""
from __future__ import annotations

import typing

import pytest

from central.central_errors.views import FrameClear, FrameImpeded
from central.displays.model import Readiness
from central.displays.views import InForceView
from central.everyday.model import AskedAction, AskState
from central.everyday.ports import AskFact, LayerOrigin, Showing, ShownPhoto
from central.frame_status.model import (
    WORST_FIRST,
    Action,
    CheckNow,
    Open,
    OpenPage,
    Override,
    OverrideKind,
    Part,
    ProblemCode,
    ProgressStep,
    RebootPi,
    RestartApp,
    Row,
    SetupStep,
    StatusWord,
)
from central.frame_status.policy import (
    CAUSES,
    CONTINUATION_LIMIT_SECONDS,
    DISPLAY_PROBLEMS,
    REBOOT_ALLOWED_SECONDS,
    ROWS,
    UNKNOWN_CAUSE,
    frame_status,
    house_status,
)
from central.frame_status.ports import (
    Dark,
    DisplayPower,
    FailingStep,
    FrameFacts,
    HouseFacts,
    Hub,
    KeptStatus,
    OnTheWay,
    Picture,
    PiHealth,
    PiHearing,
    PlanningTrouble,
    Preparation,
    Reboot,
    SourceTrouble,
    StatusKey,
    UnfilledNow,
)
from central.frame_status.views import PlayingKind, wall_can
from central.why.explain import is_resting
from central.why.model import ContentKind
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.models import Layer, Variant
from contracts.node_app_link import FrameBlackCause
from contracts.node_faults import FAULTS, FRAME_BLACK_CODES
from contracts.node_impeded import Impeded
from contracts.node_output import InForceKind, Power, PowerResult
from contracts.photo_crop import CENTRED
from contracts.room_light import CentralWords

NOW = 100_000.0
PI = "pi-1"
REACHABLE = HouseFacts(hub=Hub("reachable"))
HEARD = PiHearing(linked=True, linked_since=NOW - 500, heard_at=NOW, enrolled_at=NOW - 9000)
ON = DisplayPower(wanted=Power.ON, because="on-underneath", resting=False, result=None, due_on_at=None)
OFF = DisplayPower(wanted=Power.OFF, because="night-off", resting=True, result=PowerResult.CONFIRMED,
                   due_on_at=NOW + 3600)
SHOWING = Picture(showing=True, continuing_since=None, reported_at=NOW - 5)
BLACK = Picture(showing=False, continuing_since=None, reported_at=NOW - 5)
IMPEDED = FrameImpeded(by=Impeded(frame_id="hall", output_id="HDMI-A-1", grant_id="11111111-2222-4333-8444-555555555555",
                                  lights=("lamp",), words=CentralWords(line1="a", line2="b")))
LIBRARY = SourceTrouble("immich", "unreachable", since=NOW - 700, checked_at=NOW - 60)
SIX_PARTS = {"display", "pi", "photo app", "network", "central", "immich"}


def facts(**changes) -> FrameFacts:
    """A Frame that is set up, heard, wanted on and showing, with `changes` on top."""
    base = {"frame_id": "hall", "readiness": Readiness.READY, "device_id": PI, "power": ON, "hearing": HEARD,
            "picture": SHOWING, "reports_at": NOW - 5, "impediment": FrameClear(), "wanted": True}
    return FrameFacts(**{**base, **changes})


def status(house: HouseFacts = REACHABLE, kept: KeptStatus | None = None, **changes):
    return frame_status(facts(**changes), house, NOW, kept)


def row(house: HouseFacts = REACHABLE, **changes) -> Row:
    return status(house, **changes).because.row


# What makes each row true, on top of the Frame above.
UNBOUND = {"readiness": Readiness.UNBOUND}
HUB_DOWN = HouseFacts(hub=Hub("unreachable", NOW - 300))
INSTALLING = {"on_the_way": OnTheWay(install="restarting", install_since=NOW - 30)}
SILENT = {"hearing": PiHearing(linked=True, linked_since=NOW - 500, heard_at=NOW - SILENT_AFTER_SECONDS - 1,
                               enrolled_at=NOW - 9000)}
RESTING = {"power": OFF}
APP_GONE = {"dark": Dark("app_absent", "2026-11-22T08:10:00Z")}
DID_NOT_ANSWER = {"power": DisplayPower(wanted=Power.OFF, because="night-off", resting=False,
                                        result=PowerResult.DID_NOT_ANSWER, due_on_at=NOW + 3600)}
BLACKED = {"impediment": IMPEDED}
NOT_ON_SCREEN = {"picture": BLACK}
UNFILLED = {"unfilled": (UnfilledNow(NOW + 60, NOW + 120),)}
NO_REPORT = {"picture": None}


def test_the_table_has_the_records_thirteen_rows_and_nothing_wanted_in_order():
    """Decision 0036's thirteen, and the one it lacked, before row 10: a black Frame nothing is to play on."""
    assert [name for name, _ in ROWS] == list(Row) and len(ROWS) == 14
    assert list(Row).index(Row.NOTHING_WANTED) + 1 == list(Row).index(Row.NOT_ON_SCREEN) == 10


def test_row_1_a_frame_not_set_up_has_no_word_but_its_setup_step():
    for readiness, step in ((Readiness.UNBOUND, SetupStep.NEEDS_A_PI),
                            (Readiness.DISPLAY_CHANGED, SetupStep.DISPLAY_CHANGED),
                            (Readiness.POSITION_NEEDED, SetupStep.CHECK_POSITION)):
        found = status(readiness=readiness)
        assert (found.word, found.setup, found.because.row, found.problem, found.can) == (None, step, Row.SETUP, None, ())
    assert status().setup is None


def test_row_2_central_cannot_hear_is_cant_tell_since_then_and_row_1_wins_over_it():
    found = status(HUB_DOWN)
    assert (found.word, found.because.row, found.since) == (StatusWord.CANT_TELL, Row.CENTRAL_CANT_HEAR, NOW - 300)
    assert (found.problem.part, found.problem.code) == (Part.CENTRAL, ProblemCode.HUB_UNREACHABLE)
    stopped = status(HouseFacts(hub=Hub("worker_stopped")))
    assert (stopped.problem.code, stopped.since) == (ProblemCode.WORKER_STOPPED, NOW)
    assert row(HouseFacts(hub=Hub("no_hub"))) is Row.SHOWING      # no hub at all is not a hub that is down
    assert row(HUB_DOWN, **UNBOUND) is Row.SETUP


def test_row_3_something_on_the_way_is_getting_ready_with_its_step_and_row_2_wins_over_it():
    found = status(**INSTALLING)
    assert (found.word, found.because.row, found.problem) == (StatusWord.GETTING_READY, Row.ON_THE_WAY, None)
    assert found.progress.step is ProgressStep.INSTALL_RESTARTING
    asked = OnTheWay(reboot=Reboot(asked_at=NOW - 10, answered_at=None, delivered=None))
    assert status(on_the_way=asked).progress.step is ProgressStep.REBOOTING
    accepted = Reboot(asked_at=NOW - 40, answered_at=NOW - 30, delivered=True)
    assert status(on_the_way=OnTheWay(reboot=accepted, host_born_at=NOW - 900)).progress.step is ProgressStep.REBOOTING
    # The host started again after it accepted, the host refused, or the time allowed has passed.
    assert row(on_the_way=OnTheWay(reboot=accepted, host_born_at=NOW - 5)) is Row.SHOWING
    assert row(on_the_way=OnTheWay(reboot=Reboot(NOW - 40, NOW - 30, False))) is Row.SHOWING
    late = Reboot(NOW - REBOOT_ALLOWED_SECONDS - 40, NOW - REBOOT_ALLOWED_SECONDS - 1, True)
    assert row(on_the_way=OnTheWay(reboot=late)) is Row.SHOWING
    enrolled = PiHearing(linked=None, linked_since=None, heard_at=None, enrolled_at=NOW - 5)
    assert status(hearing=enrolled, picture=None).progress.step is ProgressStep.STARTING
    assert row(HUB_DOWN, **INSTALLING) is Row.CENTRAL_CANT_HEAR


def test_row_3_an_install_is_on_the_way_only_while_it_restarts_the_pi_and_only_for_the_time_allowed():
    """An install has a ceiling as a reboot has, and `downloading` is Central's fetch: nothing has
    happened on the Pi, so the Frame keeps its own word and a fault on it is still said."""
    downloading = OnTheWay(install="downloading", install_since=NOW - 30)
    assert row(on_the_way=downloading) is Row.SHOWING
    assert status(on_the_way=downloading, **APP_GONE).word is StatusWord.NOT_SHOWING
    at_the_limit = OnTheWay(install="restarting", install_since=NOW - REBOOT_ALLOWED_SECONDS)
    assert row(on_the_way=at_the_limit) is Row.ON_THE_WAY
    late = OnTheWay(install="restarting", install_since=NOW - REBOOT_ALLOWED_SECONDS - 1)
    assert row(on_the_way=late) is Row.SHOWING
    assert row(on_the_way=OnTheWay(install="restarting")) is Row.SHOWING      # no beginning known: no end either
    # The Pi never came back from the install's restart: Can't tell since last heard (U6), not Getting ready for days.
    gone = PiHearing(linked=False, linked_since=NOW - 86_000, heard_at=NOW - 86_400, enrolled_at=0)
    lost = status(on_the_way=OnTheWay(install="restarting", install_since=NOW - 86_400), hearing=gone)
    assert (lost.word, lost.because.row, lost.since) == (StatusWord.CANT_TELL, Row.PI_NOT_HEARD, NOW - 86_400)


def test_row_4_a_pi_not_heard_is_cant_tell_since_last_heard_part_pi_and_row_3_wins_over_it():
    found = status(**SILENT)
    assert (found.word, found.because.row) == (StatusWord.CANT_TELL, Row.PI_NOT_HEARD)
    assert (found.problem.part, found.problem.code) == (Part.PI, ProblemCode.PI_NOT_HEARD)
    assert found.since == found.problem.since == NOW - SILENT_AFTER_SECONDS - 1
    assert found.problem.fix == Open(page=OpenPage.PI, id=PI)
    at_the_limit = PiHearing(linked=True, linked_since=None, heard_at=NOW - SILENT_AFTER_SECONDS, enrolled_at=0)
    assert row(hearing=at_the_limit) is Row.SHOWING
    unlinked = status(hearing=PiHearing(linked=False, linked_since=NOW - 90, heard_at=NOW, enrolled_at=0))
    assert (unlinked.problem.code, unlinked.since) == (ProblemCode.PI_NOT_LINKED, NOW - 90)
    overdue = status(hearing=PiHearing(linked=None, linked_since=None, heard_at=None,
                                       enrolled_at=NOW - SILENT_AFTER_SECONDS - 1), picture=None)
    assert (overdue.because.row, overdue.since) == (Row.PI_NOT_HEARD, NOW - SILENT_AFTER_SECONDS - 1)
    assert row(**SILENT, **INSTALLING) is Row.ON_THE_WAY


def test_row_4_stands_aside_for_the_pis_own_verdict_while_central_holds_its_link():
    """"Heard" is the photo app's session, so a dead app is silent too. Central still reaches the
    Pi (its hub holds the link) and the Pi names the cause: Not showing, photo app, Restart app,
    and it stays so past the silence limit (one key, one `since`, one event)."""
    long_silent = PiHearing(linked=True, linked_since=NOW - 500, heard_at=NOW - 120, enrolled_at=NOW - 9000)
    found = status(hearing=long_silent, **APP_GONE)
    assert (found.word, found.because.row) == (StatusWord.NOT_SHOWING, Row.DARK)
    assert (found.problem.part, found.problem.code, found.problem.fix) == (
        Part.PHOTO_APP, ProblemCode.APP_ABSENT, RestartApp(device_id=PI))
    assert Action.RESTART_APP in found.can
    never = PiHearing(linked=True, linked_since=NOW - 500, heard_at=None, enrolled_at=NOW - 9000)
    assert row(hearing=never, **APP_GONE) is Row.DARK
    # Under Night off the same Frame is row 6: the fault is said at once.
    assert row(hearing=long_silent, **RESTING, **APP_GONE) is Row.WONT_SHOW
    # With no link, or no look at it yet, Central cannot vouch for the verdict it holds: row 4.
    for linked in (False, None):
        unheard = PiHearing(linked=linked, linked_since=None, heard_at=NOW - 120, enrolled_at=NOW - 9000)
        assert row(hearing=unheard, **APP_GONE) is Row.PI_NOT_HEARD
    # Linked and silent with no verdict: nothing names a cause, so Can't tell stands.
    assert row(hearing=long_silent) is Row.PI_NOT_HEARD


def test_row_5_a_display_off_on_purpose_is_resting_and_row_4_wins_over_it():
    found = status(**RESTING)
    assert (found.word, found.because.row, found.problem) == (StatusWord.RESTING, Row.RESTING, None)
    assert (found.because.power, found.because.until) == ("night-off", NOW + 3600)
    # A cause that ends by itself is not something wrong: still Resting.
    assert row(**RESTING, dark=Dark(FRAME_BLACK_CODES[FrameBlackCause.NOT_STARTED], "t")) is Row.RESTING
    assert row(**RESTING, **SILENT) is Row.PI_NOT_HEARD


@pytest.mark.parametrize("wrong", [APP_GONE, BLACKED, NOT_ON_SCREEN])
def test_row_6_a_failure_during_intended_darkness_needs_a_look_until_the_display_is_due_on(wrong):
    """Finding F1: Needs a look with the failing row's problem, and "Won't show at ..."; once the
    display is wanted on, the same facts are Not showing. Without the failure it is row 5."""
    found = status(**RESTING, **wrong)
    lit = status(**wrong)
    assert (found.word, found.because.row, found.because.until) == (StatusWord.NEEDS_A_LOOK, Row.WONT_SHOW, NOW + 3600)
    assert lit.word is StatusWord.NOT_SHOWING and lit.because.row in (Row.DARK, Row.CENTRAL_BLACKS, Row.NOT_ON_SCREEN)
    assert (found.problem.part, found.problem.code, found.problem.fix) == (
        lit.problem.part, lit.problem.code, lit.problem.fix)
    assert row(**RESTING) is Row.RESTING


def test_row_7_a_display_that_stayed_on_needs_a_look_part_display_before_rows_8_to_10():
    found = status(**DID_NOT_ANSWER)
    assert (found.word, found.because.row) == (StatusWord.NEEDS_A_LOOK, Row.DISPLAY_STAYED_ON)
    assert (found.problem.part, found.problem.code) == (Part.DISPLAY, ProblemCode.DISPLAY_DID_NOT_ANSWER)
    other = DisplayPower(wanted=Power.OFF, because="hold", resting=False, result=PowerResult.ANOTHER_INPUT, due_on_at=None)
    assert status(power=other).problem.code is ProblemCode.DISPLAY_ANOTHER_INPUT
    assert row(**DID_NOT_ANSWER, **APP_GONE) is Row.DISPLAY_STAYED_ON
    # The Pi has not answered the latest change yet: no display problem, the rows below speak.
    unanswered = DisplayPower(wanted=Power.OFF, because="night-off", resting=False, result=None, due_on_at=None)
    assert row(power=unanswered) is Row.SHOWING
    # Wanted on, a result alone is nothing: the row is about a display that should be off.
    on = DisplayPower(wanted=Power.ON, because="on-underneath", resting=False, result=PowerResult.DID_NOT_ANSWER,
                      due_on_at=None)
    assert row(power=on) is Row.SHOWING


def test_the_display_problems_are_the_results_the_resting_rule_refuses():
    """Row 7 names what central/why/explain.py's Resting rule judges: the two cannot drift."""
    off = InForceView(kind=InForceKind.TIMETABLE, power=Power.OFF, request_id=None, remaining_seconds=None, since=None)
    assert {result for result in PowerResult if not is_resting(Power.OFF, off, result)} == set(DISPLAY_PROBLEMS)


def test_row_8_a_dark_verdict_is_not_showing_by_its_cause_and_row_7_wins_over_it():
    found = status(**APP_GONE)
    assert (found.word, found.because.row) == (StatusWord.NOT_SHOWING, Row.DARK)
    assert (found.problem.part, found.problem.code, found.problem.fix) == (
        Part.PHOTO_APP, ProblemCode.APP_ABSENT, RestartApp(device_id=PI))
    assert (found.problem.evidence.detail, found.problem.evidence.pi_time) == ("app_absent", "2026-11-22T08:10:00Z")
    assert row(**APP_GONE, picture=SHOWING) is Row.DARK          # the verdict holds whatever the app still reports
    assert row(**{**APP_GONE, **DID_NOT_ANSWER}) is Row.DISPLAY_STAYED_ON


def test_row_9_central_blacking_the_frame_is_not_showing_part_central_and_row_8_wins_over_it():
    found = status(**BLACKED)
    assert (found.word, found.because.row) == (StatusWord.NOT_SHOWING, Row.CENTRAL_BLACKS)
    assert (found.problem.part, found.problem.code) == (Part.CENTRAL, ProblemCode.CENTRAL_BLACKS)
    assert row(**BLACKED, **APP_GONE) is Row.DARK


def test_row_10_a_picture_not_on_screen_is_not_showing_part_photo_app_and_row_9_wins_over_it():
    found = status(**NOT_ON_SCREEN)
    assert (found.word, found.because.row) == (StatusWord.NOT_SHOWING, Row.NOT_ON_SCREEN)
    assert (found.problem.part, found.problem.code, found.problem.fix) == (
        Part.PHOTO_APP, ProblemCode.PICTURE_NOT_ON_SCREEN, RestartApp(device_id=PI))
    assert row(**NOT_ON_SCREEN, **BLACKED) is Row.CENTRAL_BLACKS


def test_a_black_frame_nothing_is_to_play_on_is_resting_never_a_photo_app_failure():
    """No Scene targets the Frame now, or its Scene blacks it: the Player draws nothing and states
    no cause, on purpose. Dark on purpose is Resting; a verdict or Central's error still speaks first."""
    found = status(**NOT_ON_SCREEN, wanted=False)
    assert (found.word, found.because.row, found.problem, found.progress) == (
        StatusWord.RESTING, Row.NOTHING_WANTED, None, None)
    assert house_status((found,), REACHABLE, NOW).word == "all-good"
    assert row(**NOT_ON_SCREEN, **APP_GONE, wanted=False) is Row.DARK
    assert row(**NOT_ON_SCREEN, **BLACKED, wanted=False) is Row.CENTRAL_BLACKS
    # Under Night off it is plain Resting (row 5), not "Won't show at 07:00" every night.
    assert row(**NOT_ON_SCREEN, **RESTING, wanted=False) is Row.RESTING
    # A picture on screen is Showing whatever is wanted; no report at all is still Can't tell.
    assert row(wanted=False) is Row.SHOWING
    assert row(picture=None, wanted=False) is Row.NOTHING_KNOWN


def test_row_11_showing_with_trouble_needs_a_look_by_cause_and_row_10_wins_over_it():
    found = status(**UNFILLED)
    assert (found.word, found.because.row) == (StatusWord.NEEDS_A_LOOK, Row.SHOWING_WITH_TROUBLE)
    assert (found.problem.part, found.problem.code) == (Part.CENTRAL, ProblemCode.NO_PHOTO_PLACED)
    planning = status(planning=(PlanningTrouble("no_photo_free", "evenings", NOW - 50),))
    assert (planning.problem.part, planning.problem.code, planning.problem.fix, planning.since) == (
        Part.CENTRAL, ProblemCode.PLANNING_ISSUE, Open(page=OpenPage.SCENE, id="evenings"), NOW - 50)
    source = status(planning=(PlanningTrouble("source_empty", None, None),), scene_id="evenings")
    assert (source.problem.part, source.problem.code, source.problem.fix) == (
        Part.IMMICH, ProblemCode.SOURCE_TROUBLE, Open(page=OpenPage.SCENE, id="evenings"))
    unknown = status(planning=(PlanningTrouble("a_code_of_tomorrow", None, None),))
    assert unknown.problem.code is ProblemCode.PLANNING_ISSUE and unknown.problem.evidence.detail == "a_code_of_tomorrow"
    library = status(sources=(LIBRARY,))
    assert (library.problem.part, library.problem.code, library.problem.fix, library.since) == (
        Part.IMMICH, ProblemCode.LIBRARY_NEEDS_A_LOOK, CheckNow(connection_ref="immich"), NOW - 700)
    for health, code in ((PiHealth(throttled=True), ProblemCode.PI_THROTTLED),
                         (PiHealth(storage_low=True), ProblemCode.PI_STORAGE_LOW),
                         (PiHealth(clock_in_doubt=True), ProblemCode.PI_CLOCK_IN_DOUBT)):
        pi = status(pi_health=health)
        assert (pi.word, pi.problem.part, pi.problem.code) == (StatusWord.NEEDS_A_LOOK, Part.PI, code)
    assert row(pi_health=PiHealth()) is Row.SHOWING
    long = Picture(showing=True, continuing_since=NOW - CONTINUATION_LIMIT_SECONDS - 1, reported_at=NOW - 5)
    continuing = status(picture=long)
    assert (continuing.problem.code, continuing.since) == (
        ProblemCode.CONTINUING_PAST_LIMIT, NOW - CONTINUATION_LIMIT_SECONDS - 1)
    within = Picture(showing=True, continuing_since=NOW - CONTINUATION_LIMIT_SECONDS, reported_at=NOW - 5)
    assert row(picture=within) is Row.SHOWING
    # The same trouble on a Frame whose picture is not on screen: row 10 speaks.
    assert row(**UNFILLED, **NOT_ON_SCREEN) is Row.NOT_ON_SCREEN


def test_row_12_showing_for_centrals_generation_is_showing_and_row_11_wins_over_it():
    found = status()
    assert (found.word, found.because.row, found.problem, found.progress) == (StatusWord.SHOWING, Row.SHOWING, None, None)
    assert row(**UNFILLED) is Row.SHOWING_WITH_TROUBLE


def test_row_13_nothing_known_is_cant_tell_since_the_last_report_part_pi():
    found = status(**NO_REPORT)
    assert (found.word, found.because.row, found.since) == (StatusWord.CANT_TELL, Row.NOTHING_KNOWN, NOW - 5)
    assert (found.problem.part, found.problem.code) == (Part.PI, ProblemCode.NO_REPORT)
    never = status(picture=None, reports_at=None)
    assert (never.because.row, never.since) == (Row.NOTHING_KNOWN, NOW)
    assert row(picture=SHOWING) is Row.SHOWING


# --- The cause table ----------------------------------------------------------------------------

def test_the_cause_table_has_a_row_for_every_fault_code_and_every_frame_black_cause():
    """A new code without a row fails here. Mutation probe: drop any row of CAUSES."""
    assert set(CAUSES) == set(FAULTS)
    assert {FRAME_BLACK_CODES[cause] for cause in FrameBlackCause} <= set(CAUSES)


EXPECTED = {
    **dict.fromkeys(("app_absent", "app_unresponsive", "app_resource_exhausted", "app_descriptor_limit"),
                    (StatusWord.NOT_SHOWING, Part.PHOTO_APP, RestartApp(device_id=PI))),
    **dict.fromkeys(("frame_no_renderer", "frame_cannot_draw"), (StatusWord.NOT_SHOWING, Part.PI, RebootPi(device_id=PI))),
    "software_renderer": (StatusWord.NEEDS_A_LOOK, Part.PI, RebootPi(device_id=PI)),
    **dict.fromkeys(("screen_not_told", "no_frame_placed", "frame_no_plan"),
                    (StatusWord.NOT_SHOWING, Part.CENTRAL, Open(page=OpenPage.FRAME, id="hall"))),
    "frame_no_time": (StatusWord.NOT_SHOWING, Part.PI, Open(page=OpenPage.PI, id=PI)),
}
NO_PHOTO_CODES = ("frame_no_photo", "frame_no_content", "nothing_to_show")


def test_decision_0036s_cause_table_row_by_row():
    assert set(EXPECTED) | {"frame_not_started", *NO_PHOTO_CODES} == set(FAULTS)
    for code, (word, part, fix) in EXPECTED.items():
        found = status(dark=Dark(code, "t"))
        assert (found.word, found.problem.part, found.problem.fix) == (word, part, fix), code
        assert found.problem.evidence.detail == code
    started = status(dark=Dark("frame_not_started", "t"))
    assert (started.word, started.problem, started.progress.step) == (
        StatusWord.GETTING_READY, None, ProgressStep.WAITING_TO_START)


@pytest.mark.parametrize("code", NO_PHOTO_CODES)
def test_no_photo_is_getting_ready_while_photos_are_being_prepared_else_needs_a_look(code):
    """U2's per-Frame progress: the step and its count."""
    being = status(dark=Dark(code, "t"), preparation=Preparation(planned=5, prepared=2, failed=0))
    assert (being.word, being.problem) == (StatusWord.GETTING_READY, None)
    assert (being.progress.step, being.progress.done, being.progress.total) == (ProgressStep.PREPARING_PHOTOS, 2, 5)
    for held in (None, Preparation(0, 0, 0), Preparation(5, 5, 0), Preparation(5, 2, 1)):
        found = status(dark=Dark(code, "t"), preparation=held, scene_id="evenings")
        assert (found.word, found.problem.code, found.problem.part, found.problem.fix) == (
            StatusWord.NEEDS_A_LOOK, ProblemCode.NO_PHOTOS, Part.CENTRAL, Open(page=OpenPage.SCENE, id="evenings"))
    assert status(dark=Dark(code, "t"), sources=(LIBRARY,)).problem.part is Part.IMMICH
    assert status(dark=Dark(code, "t"), planning=(PlanningTrouble("source_empty", None, None),)).problem.part is Part.IMMICH


def test_a_dark_code_the_table_lacks_is_still_not_showing():
    found = status(dark=Dark("a_code_of_tomorrow", "t"))
    assert (found.word, found.problem.part, found.problem.code) == (
        UNKNOWN_CAUSE.word, Part.PI, ProblemCode.DARK_UNKNOWN) == (StatusWord.NOT_SHOWING, Part.PI, ProblemCode.DARK_UNKNOWN)


def test_a_fix_on_the_pi_with_no_pi_known_opens_the_frame():
    assert status(**APP_GONE, device_id=None).problem.fix == Open(page=OpenPage.FRAME, id="hall")


EVERY_PROBLEM = [
    (HUB_DOWN, {}), (HouseFacts(hub=Hub("worker_stopped")), {}), (REACHABLE, SILENT), (REACHABLE, {**RESTING, **APP_GONE}),
    (REACHABLE, DID_NOT_ANSWER), (REACHABLE, BLACKED), (REACHABLE, NOT_ON_SCREEN), (REACHABLE, UNFILLED),
    (REACHABLE, {"sources": (LIBRARY,)}), (REACHABLE, {"pi_health": PiHealth(throttled=True)}), (REACHABLE, NO_REPORT),
    *((REACHABLE, {"dark": Dark(code, "t")}) for code in (*FAULTS, "a_code_of_tomorrow")),
]


def test_every_problem_names_one_of_the_six_parts_and_a_frame_and_comes_with_its_word():
    assert {part.value for part in Part} == SIX_PARTS
    assert {cause.part.value for cause in CAUSES.values() if cause.part is not None} <= SIX_PARTS
    for house, changes in EVERY_PROBLEM:
        found = status(house, **changes)
        with_problem = found.word in (StatusWord.NOT_SHOWING, StatusWord.CANT_TELL, StatusWord.NEEDS_A_LOOK)
        assert (found.problem is not None) == with_problem, changes
        assert (found.progress is not None) <= (found.word is StatusWord.GETTING_READY)
        if found.problem is not None:
            assert found.problem.part.value in SIX_PARTS and found.problem.frame_id == "hall"
            assert found.problem.part is not Part.NETWORK      # Central never claims the network (U6)


# --- Since when, the actions, the override ------------------------------------------------------

def test_since_stands_while_the_key_stands_and_begins_again_when_it_changes():
    showing = StatusKey(StatusWord.SHOWING, None, Row.SHOWING, None)
    assert status(kept=KeptStatus(showing, NOW - 4000)).since == NOW - 4000
    assert status().since == NOW
    changed = status(kept=KeptStatus(showing, NOW - 4000), **NOT_ON_SCREEN)
    assert (changed.since, changed.problem.since) == (NOW, NOW)
    # The same word for another problem is another key.
    blacked = StatusKey(StatusWord.NOT_SHOWING, None, Row.CENTRAL_BLACKS, ProblemCode.CENTRAL_BLACKS)
    assert status(kept=KeptStatus(blacked, NOW - 4000), **NOT_ON_SCREEN).since == NOW
    # A kept row wins over the fact's own time once it is kept: the row is the one record.
    silent = StatusKey(StatusWord.CANT_TELL, None, Row.PI_NOT_HEARD, ProblemCode.PI_NOT_HEARD)
    assert status(kept=KeptStatus(silent, NOW - 77), **SILENT).since == NOW - 77


def test_can_offers_the_pi_s_actions_only_while_central_holds_its_link():
    assert status().can == (Action.REBOOT_PI, Action.RESTART_APP)
    assert status(**APP_GONE).can == (Action.REBOOT_PI, Action.RESTART_APP)
    assert status(HUB_DOWN).can == ()
    assert status(hearing=PiHearing(linked=None, linked_since=None, heard_at=NOW, enrolled_at=0)).can == ()
    assert status(device_id=None).can == ()
    assert status(readiness=Readiness.POSITION_NEEDED).can == ()


def test_a_card_never_offers_what_the_frames_menu_may_not():
    """A fix asked of the Pi (Restart app, Reboot the Pi) that `can` does not hold now, because no
    hub is configured or the Pi's link is not known, is a link to the Pi instead."""
    for house, hearing in ((HouseFacts(hub=Hub("no_hub")), HEARD),
                           (REACHABLE, PiHearing(linked=None, linked_since=None, heard_at=NOW, enrolled_at=0))):
        for changes in (APP_GONE, NOT_ON_SCREEN, {"dark": Dark("frame_cannot_draw", "t")}):
            found = status(house, hearing=hearing, **changes)
            assert (found.can, found.problem.fix) == ((), Open(page=OpenPage.PI, id=PI)), changes
    assert status(**APP_GONE).problem.fix == RestartApp(device_id=PI)


def test_an_override_rides_on_the_status_and_a_paused_frame_is_showing():
    """Finding F3: a pause is an override on a Frame that reads Showing."""
    pause = Override(kind=OverrideKind.PAUSE, id="p-1", frame_ids=("hall",), until=NOW + 3600)
    found = status(override=pause)
    assert (found.word, found.override) == (StatusWord.SHOWING, pause)


# --- Slice 3: the everyday actions ----------------------------------------------------------------

def _photo_layer(key: str, start: float, end: float, asset: str) -> Layer:
    return Layer(assignment_id=key, run_id="run", output_id="HDMI-A-1", frame_id="hall", binding_generation=1,
                 start=start, end=end, media_origin=start, crop=CENTRED, variant=Variant(
                     asset_id=asset, recipe_id="e" * 64, sha256="a" * 64, size=10, media_type="image/jpeg",
                     width=1920, height=1080))


TURNS = (_photo_layer("a", NOW - 10, NOW + 20, "photo-a"), _photo_layer("b", NOW + 20, NOW + 50, "photo-b"))
ON_SCREEN = Showing(layers=TURNS, origins={layer.assignment_id: LayerOrigin(scene_id="s") for layer in TURNS})


def test_can_follows_the_stay_rule():
    """Skip and Previous are in `can` exactly where central/everyday/policy.py `stays` lets the
    Frame move, and Never show while it shows a photo; nothing without what it shows (the status
    step's reading). Mutation probe: offer Skip without asking `stays`."""
    assert status(showing=ON_SCREEN).can == (Action.NEVER_SHOW, Action.PAUSE, Action.REBOOT_PI, Action.RESTART_APP,
                                             Action.SKIP)
    earlier = Showing(layers=TURNS, origins=ON_SCREEN.origins, history=(
        ShownPhoto("photo-a", "a", NOW - 9), ShownPhoto("photo-z", "z", NOW - 40)))
    assert Action.PREVIOUS in status(showing=earlier).can
    hand = Showing(layers=TURNS, origins={"a": LayerOrigin(scene_id="s", hand_placed=True)})
    assert Action.SKIP not in status(showing=hand).can and Action.NEVER_SHOW in status(showing=hand).can
    for changes in (SILENT, RESTING, {"picture": Picture(showing=True, continuing_since=NOW - 30,
                                                          reported_at=NOW - 5)}):
        assert Action.SKIP not in status(showing=ON_SCREEN, **changes).can, changes
    assert Action.SKIP in status(HUB_DOWN, showing=ON_SCREEN).can      # Skip needs no hub
    assert status().can == (Action.REBOOT_PI, Action.RESTART_APP)
    assert status(showing=ON_SCREEN, readiness=Readiness.POSITION_NEEDED).can == ()


def test_a_frame_names_the_photo_it_shows():
    """`photo` is the photo drawn now, what the console's Never show sends; none in setup, with
    nothing drawn, or without what it shows. Mutation probe: name the next turn's photo."""
    assert status(showing=ON_SCREEN).photo == "photo-a"
    assert status(showing=Showing()).photo is None
    assert status().photo is None
    assert status(showing=ON_SCREEN, readiness=Readiness.POSITION_NEEDED).photo is None


def test_a_walls_can_is_any_of_its_frames():
    moving = status(showing=ON_SCREEN)
    still = status(showing=Showing())
    assert wall_can((moving, still)) == (Action.SKIP,)
    assert wall_can((still,)) == ()


def test_asked_is_reported_while_its_cover_shows():
    fact = AskFact(action=AskedAction.SKIP, request_id="r", at=NOW - 1, until=NOW + 29)
    asked = status(showing=Showing(layers=TURNS, asked=fact)).asked
    assert (asked.action, asked.state) == (AskedAction.SKIP, AskState.REQUESTED)
    done = AskFact(action=AskedAction.SKIP, request_id="r", at=NOW - 1, until=NOW + 29, presented_at=NOW)
    assert status(showing=Showing(asked=done)).asked.state is AskState.DONE
    over = AskFact(action=AskedAction.SKIP, request_id="r", at=NOW - 60, until=NOW - 1)
    assert status(showing=Showing(asked=over)).asked is None


def test_the_playing_kinds_are_whys_content_kinds():
    assert set(typing.get_args(PlayingKind)) == set(typing.get_args(ContentKind))


# --- The house ------------------------------------------------------------------------------------

def frame(frame_id: str, house: HouseFacts = REACHABLE, **changes):
    return frame_status(facts(frame_id=frame_id, **changes), house, NOW)


def test_the_house_word_is_the_worst_frame_word_and_all_good_when_each_shows_or_rests():
    assert WORST_FIRST[:4] == (StatusWord.NOT_SHOWING, StatusWord.CANT_TELL, StatusWord.NEEDS_A_LOOK,
                               StatusWord.GETTING_READY)
    good = (frame("a"), frame("b", **RESTING))
    assert house_status(good, REACHABLE, NOW).word == "all-good"
    assert house_status((), REACHABLE, NOW).word == "all-good"
    ladder = [(INSTALLING, StatusWord.GETTING_READY), (UNFILLED, StatusWord.NEEDS_A_LOOK),
              (NO_REPORT, StatusWord.CANT_TELL), (NOT_ON_SCREEN, StatusWord.NOT_SHOWING)]
    frames = list(good)
    for index, (changes, word) in enumerate(ladder):
        frames.append(frame(f"f{index}", **changes))
        assert house_status(tuple(frames), REACHABLE, NOW).word == word
    assert house_status(tuple(reversed(frames)), REACHABLE, NOW).word == StatusWord.NOT_SHOWING


def test_a_frame_in_setup_does_not_count_and_has_no_problem_on_home():
    house = house_status((frame("a"), frame("b", **UNBOUND)), REACHABLE, NOW)
    assert (house.word, house.problems) == ("all-good", ())


def test_homes_problems_are_worst_first():
    frames = (frame("a", **UNFILLED), frame("b", **NOT_ON_SCREEN), frame("c", **NO_REPORT), frame("d"))
    assert [problem.frame_id for problem in house_status(frames, REACHABLE, NOW).problems] == ["b", "c", "a"]


def test_the_hub_down_is_one_house_problem_that_replaces_every_frame_problem_it_explains():
    """One banner, not twelve cards. A Frame in setup is not explained: it had no problem."""
    frames = tuple(frame(f"f{index:02d}", HUB_DOWN) for index in range(12)) + (frame("new", HUB_DOWN, **UNBOUND),)
    house = house_status(frames, HUB_DOWN, NOW)
    assert house.word == StatusWord.CANT_TELL and len(house.problems) == 1
    (problem,) = house.problems
    assert (problem.frame_id, problem.part, problem.code, problem.since) == (
        None, Part.CENTRAL, ProblemCode.HUB_UNREACHABLE, NOW - 300)
    assert problem.explains == tuple(f"f{index:02d}" for index in range(12))
    assert all(each.problem is not None for each in frames[:12])       # each Frame still says it on its own page


def test_no_library_connection_answering_is_one_house_problem_and_one_of_two_is_each_frames_own():
    frames = (frame("a", sources=(LIBRARY,)), frame("b", sources=(LIBRARY,)), frame("c", **NOT_ON_SCREEN))
    every = house_status(frames, HouseFacts(hub=Hub("reachable"), libraries=(LIBRARY,), library_count=1), NOW)
    # Worst first across the house's and the Frames' own: Not showing before the house's Needs a look.
    assert [(problem.frame_id, problem.code) for problem in every.problems] == [
        ("c", ProblemCode.PICTURE_NOT_ON_SCREEN), (None, ProblemCode.NO_LIBRARY_ANSWERS)]
    assert every.problems[1].explains == ("a", "b") and every.problems[1].part is Part.IMMICH
    assert every.word == StatusWord.NOT_SHOWING
    one = house_status(frames, HouseFacts(hub=Hub("reachable"), libraries=(LIBRARY,), library_count=2), NOW)
    assert [problem.frame_id for problem in one.problems] == ["c", "a", "b"]


def test_a_failing_scheduler_and_a_planning_issue_naming_no_frame_are_house_problems():
    house = HouseFacts(hub=Hub("reachable"), failing_steps=(FailingStep("planning", NOW - 40), FailingStep("status", NOW - 9)),
                       planning=(PlanningTrouble("intent_refused", "evenings", NOW - 20),))
    found = house_status((frame("a"),), house, NOW)
    assert found.word == StatusWord.NEEDS_A_LOOK
    assert [(problem.frame_id, problem.code, problem.part, problem.since, problem.evidence.detail) for problem in found.problems] == [
        (None, ProblemCode.SCHEDULER_FAILING, Part.CENTRAL, NOW - 40, "planning,status"),
        (None, ProblemCode.PLANNING_ISSUE, Part.CENTRAL, NOW - 20, "intent_refused")]
    assert all(problem.part.value in SIX_PARTS for problem in found.problems)


def test_pause_is_in_can_by_the_pause_rule():
    """Pause is offered where central/everyday/policy.py `pause_refusal` would pause the Frame:
    never on a video (the owner's S8), nor on a Frame already paused, resting or silent; Skip
    stays away from a paused Frame. Mutation probe: offer Pause without asking `pause_refusal`."""
    film = _photo_layer("film", NOW - 10, NOW + 20, "film").model_copy(update={"variant": Variant(
        asset_id="film", recipe_id="e" * 64, sha256="a" * 64, size=10, media_type="video/mp4",
        width=1920, height=1080, duration=30.0)})
    assert Action.PAUSE not in status(showing=Showing(layers=(film,))).can
    pause = Override(kind=OverrideKind.PAUSE, id="p-1", frame_ids=("hall",), until=NOW + 3600)
    paused = status(showing=ON_SCREEN, override=pause, paused=True)
    assert Action.PAUSE not in paused.can and Action.SKIP not in paused.can
    for changes in (SILENT, RESTING):
        assert Action.PAUSE not in status(showing=ON_SCREEN, **changes).can, changes


def test_a_paused_frame_reads_showing_with_its_pause():
    """Finding F3: a paused Frame holds a lit photo, so it reads Showing with its pause as its
    override, not Resting."""
    pause = Override(kind=OverrideKind.PAUSE, id="p-1", frame_ids=("hall",), by="person:matt",
                     started_at=NOW - 60, until=NOW + 3540)
    found = status(showing=ON_SCREEN, override=pause, paused=True)
    assert (found.word, found.override, found.override.by) == (StatusWord.SHOWING, pause, "person:matt")
