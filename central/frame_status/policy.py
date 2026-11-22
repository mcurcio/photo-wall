"""A Frame's status: the decision table, the cause table and the house word (decision 0036), their
one home. Pure: every fact is an argument (ports.py), judged by its owner before it gets here; this
module only chooses which fact speaks first and what it means for the Frame.

- **The decision table** is `ROWS`, in order; the first row that matches wins (`frame_status`).
  The record's thirteen rows, and one it lacked: a black Frame nothing is to play on (`_nothing_wanted`).
- **The cause table** is `CAUSES`: every code of contracts/node_faults.py (every `FrameBlackCause`
  among them) to a word, a part and a fix. A test holds it exhaustive.
- **Since when**: a Frame's status keeps the `since` of its kept row while its key (word, setup
  step, row, problem code) stands; a new key begins at the time its row's own fact states (the
  hub's last change, when the Pi was last heard, the last report), else at `now`.
- **The house word** is `house_status`.

The limits of finding O3: `SILENT_AFTER_SECONDS` (contracts/liveness.py, the console's threshold
until now) for row 4, `CONTINUATION_LIMIT_SECONDS` for row 11.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from central.central_errors.views import FrameImpeded
from central.displays.model import Readiness
from central.frame_status.model import (
    ALL_GOOD,
    GOOD,
    WORST_FIRST,
    Action,
    Because,
    CheckNow,
    DoingCode,
    Evidence,
    EvidenceLayer,
    Fix,
    FrameStatus,
    HouseStatus,
    HouseWord,
    Open,
    OpenPage,
    Part,
    Problem,
    ProblemCode,
    Progress,
    ProgressStep,
    RebootPi,
    RestartApp,
    Row,
    SetupStep,
    StatusWord,
)
from central.frame_status.ports import FrameFacts, HouseFacts, KeptStatus, PlanningTrouble, key_of
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.node_app_link import FrameBlackCause
from contracts.node_faults import FRAME_BLACK_CODES
from contracts.node_output import Power, PowerResult

# Row 11: how long a Frame may play Central's continuation before it needs a look (O3: to tune).
CONTINUATION_LIMIT_SECONDS: Final = 3600.0
# Row 3: how long an asked or accepted reboot, or an install's restart, counts as in progress with
# no new start of the host. Past it the rows below speak: a Pi that never came back is row 4's.
REBOOT_ALLOWED_SECONDS: Final = 600.0

_SETUP: Final[Mapping[Readiness, SetupStep]] = MappingProxyType({
    Readiness.UNBOUND: SetupStep.NEEDS_A_PI, Readiness.DISPLAY_CHANGED: SetupStep.DISPLAY_CHANGED,
    Readiness.POSITION_NEEDED: SetupStep.CHECK_POSITION})
_HUB_PROBLEMS: Final[Mapping[str, ProblemCode]] = MappingProxyType({
    "unreachable": ProblemCode.HUB_UNREACHABLE, "worker_stopped": ProblemCode.WORKER_STOPPED})
# Row 7: the power results after which the display is not dark on purpose (central/why/explain.py's
# Resting rule judges; this names each as a problem).
DISPLAY_PROBLEMS: Final[Mapping[PowerResult, ProblemCode]] = MappingProxyType({
    PowerResult.DID_NOT_ANSWER: ProblemCode.DISPLAY_DID_NOT_ANSWER,
    PowerResult.ANOTHER_INPUT: ProblemCode.DISPLAY_ANOTHER_INPUT})
# Row 11: the Planner's codes that say a Scene's Photo source could not be read; every other code,
# one this Central does not know among them, is a planning issue.
SOURCE_PLANNING_CODES: Final[frozenset[str]] = frozenset({
    "source_missing", "source_empty", "source_not_ready", "asset_preparation_failed"})


class CauseFix(StrEnum):
    """The cause table's fix column, before it is given its Frame's ids."""
    RESTART_APP = "restart-app"
    REBOOT_PI = "reboot-pi"
    OPEN_FRAME = "open-frame"
    OPEN_PI = "open-pi"
    OPEN_PHOTO_SOURCE = "open-photo-source"


@dataclass(frozen=True, slots=True)
class Cause:
    """One row of the cause table. `preparing`: Getting ready while photos are being prepared for
    the Frame, else this row's word and problem; `progress`: the step of a cause that ends by
    itself (its word is Getting ready and it has no problem)."""
    word: StatusWord
    part: Part | None = None
    code: ProblemCode | None = None
    fix: CauseFix | None = None
    preparing: bool = False
    progress: ProgressStep | None = None


def _frame(cause: FrameBlackCause) -> str:
    return FRAME_BLACK_CODES[cause]


_NO_PHOTO: Final = Cause(StatusWord.NEEDS_A_LOOK, Part.IMMICH, ProblemCode.NO_PHOTOS, CauseFix.OPEN_PHOTO_SOURCE,
                         preparing=True)
_NO_INSTRUCTION: Final = Cause(StatusWord.NOT_SHOWING, Part.CENTRAL, ProblemCode.NO_INSTRUCTION, CauseFix.OPEN_FRAME)
CAUSES: Final[Mapping[str, Cause]] = MappingProxyType({
    "app_absent": Cause(StatusWord.NOT_SHOWING, Part.PHOTO_APP, ProblemCode.APP_ABSENT, CauseFix.RESTART_APP),
    "app_unresponsive": Cause(StatusWord.NOT_SHOWING, Part.PHOTO_APP, ProblemCode.APP_UNRESPONSIVE, CauseFix.RESTART_APP),
    "app_resource_exhausted": Cause(StatusWord.NOT_SHOWING, Part.PHOTO_APP, ProblemCode.APP_RESOURCE_EXHAUSTED,
                                    CauseFix.RESTART_APP),
    "app_descriptor_limit": Cause(StatusWord.NOT_SHOWING, Part.PHOTO_APP, ProblemCode.APP_DESCRIPTOR_LIMIT,
                                  CauseFix.RESTART_APP),
    _frame(FrameBlackCause.NO_RENDERER): Cause(StatusWord.NOT_SHOWING, Part.PI, ProblemCode.CANNOT_DRAW, CauseFix.REBOOT_PI),
    _frame(FrameBlackCause.CANNOT_DRAW): Cause(StatusWord.NOT_SHOWING, Part.PI, ProblemCode.CANNOT_DRAW, CauseFix.REBOOT_PI),
    # Not dark: the Frame shows, drawn without the graphics processor.
    "software_renderer": Cause(StatusWord.NEEDS_A_LOOK, Part.PI, ProblemCode.SOFTWARE_RENDERER, CauseFix.REBOOT_PI),
    "screen_not_told": _NO_INSTRUCTION,
    "no_frame_placed": _NO_INSTRUCTION,
    _frame(FrameBlackCause.NO_PLAN): _NO_INSTRUCTION,
    _frame(FrameBlackCause.NO_TIME): Cause(StatusWord.NOT_SHOWING, Part.PI, ProblemCode.PI_CLOCK, CauseFix.OPEN_PI),
    _frame(FrameBlackCause.NOT_STARTED): Cause(StatusWord.GETTING_READY, progress=ProgressStep.WAITING_TO_START),
    _frame(FrameBlackCause.NO_PHOTO): _NO_PHOTO,
    _frame(FrameBlackCause.NO_CONTENT): _NO_PHOTO,
    "nothing_to_show": _NO_PHOTO,
})
# A dark code this Central's table lacks (a newer Pi's): dark all the same.
UNKNOWN_CAUSE: Final = Cause(StatusWord.NOT_SHOWING, Part.PI, ProblemCode.DARK_UNKNOWN, CauseFix.OPEN_PI)


@dataclass(frozen=True, slots=True)
class _Trouble:
    """A problem before its Frame's `since` is known; `since`: the fact's own time, when it has one."""
    part: Part
    code: ProblemCode
    evidence: Evidence
    fix: Fix | None = None
    doing: DoingCode | None = None
    since: float | None = None


@dataclass(frozen=True, slots=True)
class _Found:
    """What a matching row says. `began`: when the row's own fact says this began (None: when
    Central first saw it)."""
    word: StatusWord | None
    because: Because
    setup: SetupStep | None = None
    trouble: _Trouble | None = None
    progress: Progress | None = None
    began: float | None = None


_RowRule = Callable[[FrameFacts, HouseFacts, float], _Found | None]


def _open(page: OpenPage, target: str | None) -> Fix:
    return Open(page=page, id=target)


def _open_pi(facts: FrameFacts) -> Fix:
    return _open(OpenPage.PI, facts.device_id) if facts.device_id is not None else _open(OpenPage.FRAME, facts.frame_id)


def _open_photo_source(facts: FrameFacts, scene_id: str | None = None) -> Fix:
    scene = scene_id if scene_id is not None else facts.scene_id
    return _open(OpenPage.SCENE, scene) if scene is not None else _open(OpenPage.FRAME, facts.frame_id)


def _cause_fix(fix: CauseFix | None, facts: FrameFacts) -> Fix | None:
    """A cause's fix with its Frame's ids; a fix on the Pi with no Pi known opens the Frame."""
    if fix is None:
        return None
    if fix is CauseFix.OPEN_PHOTO_SOURCE:
        return _open_photo_source(facts)
    if fix is CauseFix.OPEN_PI:
        return _open_pi(facts)
    if fix is CauseFix.OPEN_FRAME or facts.device_id is None:
        return _open(OpenPage.FRAME, facts.frame_id)
    return RestartApp(device_id=facts.device_id) if fix is CauseFix.RESTART_APP else RebootPi(device_id=facts.device_id)


# --- The rows ---------------------------------------------------------------------------------------

def _setup(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """1: the Frame is not set up."""
    step = _SETUP.get(facts.readiness)
    return None if step is None else _Found(None, Because(row=Row.SETUP), setup=step)


def _central_cant_hear(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """2: Central's hub is unreachable or its worker stopped."""
    code = _HUB_PROBLEMS.get(house.hub.state)
    if code is None:
        return None
    return _Found(StatusWord.CANT_TELL, Because(row=Row.CENTRAL_CANT_HEAR), began=house.hub.since, trouble=_Trouble(
        Part.CENTRAL, code, Evidence(layer=EvidenceLayer.HUB, detail=house.hub.state, at=house.hub.since),
        fix=_open(OpenPage.CENTRAL, None), doing=DoingCode.RECONNECTING, since=house.hub.since))


def rebooting(facts: FrameFacts, now: float) -> bool:
    """Whether a reboot of the Frame's Pi is in progress: asked and not yet run, or accepted by the
    host and the host not started again since; in either case for REBOOT_ALLOWED_SECONDS at most."""
    reboot = facts.on_the_way.reboot
    if reboot is None:
        return False
    if reboot.answered_at is None:
        return reboot.asked_at is not None and now - reboot.asked_at <= REBOOT_ALLOWED_SECONDS
    born = facts.on_the_way.host_born_at
    return (reboot.delivered is True and (born is None or born <= reboot.answered_at)
            and now - reboot.answered_at <= REBOOT_ALLOWED_SECONDS)


def installing(facts: FrameFacts, now: float) -> bool:
    """Whether an install is restarting the Frame's Pi: its `restarting` step, for
    REBOOT_ALLOWED_SECONDS at most from when the step began (a step with no known beginning is not
    counted: it has no end either). `downloading` is Central fetching the release: nothing has
    happened on the Pi yet, so the rows below speak."""
    way = facts.on_the_way
    return (way.install == "restarting" and way.install_since is not None
            and now - way.install_since <= REBOOT_ALLOWED_SECONDS)


def _on_the_way(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """3: a reboot of its Pi or an install's restart is in progress (each for REBOOT_ALLOWED_SECONDS
    at most), or the Pi enrolled and has not reported yet."""
    hearing = facts.hearing
    if installing(facts, now):
        step = ProgressStep.INSTALL_RESTARTING
    elif rebooting(facts, now):
        step = ProgressStep.REBOOTING
    elif hearing is not None and hearing.heard_at is None and now - hearing.enrolled_at <= SILENT_AFTER_SECONDS:
        step = ProgressStep.STARTING
    else:
        return None
    return _Found(StatusWord.GETTING_READY, Because(row=Row.ON_THE_WAY), progress=Progress(step=step))


def _pi_not_heard(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """4: its Pi is not linked, or not heard past the silence limit. Never "network": Central
    cannot tell a dead Pi from a pulled cable, so it says when it last heard.

    "Heard" is the photo app's session (central/infra/player_contact_store.py), so a dead or
    frozen app is silent too. While Central's hub holds the Pi's link and the Pi's own verdict says
    its screen is dark, Central does hear the Pi and the Pi names the cause: this row stands aside
    and the verdict speaks (rows 6 and 8), so a dead app stays "Not showing: photo app"."""
    hearing = facts.hearing
    if hearing is not None and hearing.linked is True and facts.dark is not None:
        return None
    if hearing is None:
        code, since = ProblemCode.PI_NOT_HEARD, None
    elif hearing.heard_at is None:
        code, since = ProblemCode.PI_NOT_HEARD, hearing.enrolled_at
    elif now - hearing.heard_at > SILENT_AFTER_SECONDS:
        code, since = ProblemCode.PI_NOT_HEARD, hearing.heard_at
    elif hearing.linked is False:
        code, since = ProblemCode.PI_NOT_LINKED, hearing.linked_since
    else:
        return None
    return _Found(StatusWord.CANT_TELL, Because(row=Row.PI_NOT_HEARD), began=since, trouble=_Trouble(
        Part.PI, code, Evidence(layer=EvidenceLayer.PRESENCE, at=since), fix=_open_pi(facts),
        doing=DoingCode.RECONNECTING, since=since))


def _off_and_carried_out(facts: FrameFacts) -> bool:
    return facts.power is not None and facts.power.wanted is Power.OFF and facts.power.resting


def _wrong_while_on(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """What rows 8 to 10 find wrong (a cause that ends by itself is not wrong)."""
    for rule in (_dark, _central_blacks, _not_on_screen):
        found = rule(facts, house, now)
        if found is not None:
            return found if found.trouble is not None else None
    return None


def _resting(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """5: its display should be off and the Pi carried that out, and nothing in rows 8 to 10 is wrong."""
    if not _off_and_carried_out(facts) or _wrong_while_on(facts, house, now) is not None:
        return None
    return _Found(StatusWord.RESTING, Because(row=Row.RESTING, power=facts.power.because, until=facts.power.due_on_at))


def _wont_show(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """6 (finding F1): its display should be off, and something in rows 8 to 10 is wrong: Needs a
    look until the display is next due on, when rows 8 to 10 say Not showing."""
    wrong = _wrong_while_on(facts, house, now) if _off_and_carried_out(facts) else None
    if wrong is None:
        return None
    return _Found(StatusWord.NEEDS_A_LOOK, trouble=wrong.trouble, began=wrong.began,
                  because=Because(row=Row.WONT_SHOW, power=facts.power.because, until=facts.power.due_on_at))


def _display_stayed_on(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """7: its display should be off and the display did not answer, or shows another input."""
    power = facts.power
    if power is None or power.wanted is not Power.OFF or power.resting or power.result not in DISPLAY_PROBLEMS:
        return None
    return _Found(StatusWord.NEEDS_A_LOOK, Because(row=Row.DISPLAY_STAYED_ON, power=power.because, until=power.due_on_at),
                  trouble=_Trouble(Part.DISPLAY, DISPLAY_PROBLEMS[power.result],
                                   Evidence(layer=EvidenceLayer.POWER_REPORT, detail=power.result.value,
                                            at=power.reported_at),
                                   fix=_open(OpenPage.FRAME, facts.frame_id)))


def preparing(facts: FrameFacts) -> bool:
    """Whether photos are being prepared for the Frame: its Player's plan holds photos for it, not
    all are prepared yet and none failed."""
    held = facts.preparation
    return held is not None and held.planned > 0 and held.prepared < held.planned and held.failed == 0


def _dark(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """8: the Pi's verdict says dark, by cause (`CAUSES`)."""
    if facts.dark is None:
        return None
    cause = CAUSES.get(facts.dark.code, UNKNOWN_CAUSE)
    because = Because(row=Row.DARK)
    if cause.preparing and preparing(facts):
        held = facts.preparation
        return _Found(StatusWord.GETTING_READY, because, progress=Progress(
            step=ProgressStep.PREPARING_PHOTOS, done=held.prepared, total=held.planned))
    if cause.code is None or cause.part is None:
        return _Found(cause.word, because, progress=None if cause.progress is None else Progress(step=cause.progress))
    part = cause.part
    if cause.preparing and not (facts.sources or any(each.code in SOURCE_PLANNING_CODES for each in facts.planning)):
        part = Part.CENTRAL       # no Photo source is in trouble: the plan holds no photo for it
    return _Found(cause.word, because, trouble=_Trouble(
        part, cause.code, Evidence(layer=EvidenceLayer.HEALTH_VERDICT, detail=facts.dark.code, pi_time=facts.dark.pi_since),
        fix=_cause_fix(cause.fix, facts)))


def _central_blacks(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """9: Central blacks the Frame (its impediment)."""
    if not isinstance(facts.impediment, FrameImpeded):
        return None
    return _Found(StatusWord.NOT_SHOWING, Because(row=Row.CENTRAL_BLACKS), trouble=_Trouble(
        Part.CENTRAL, ProblemCode.CENTRAL_BLACKS,
        Evidence(layer=EvidenceLayer.IMPEDIMENT, detail=facts.impediment.by.lights[0]),
        fix=_open(OpenPage.FRAME, facts.frame_id)))


def _nothing_wanted(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """10, as built: the Frame report says no picture is on screen, the Pi's verdict names nothing
    wrong, and nothing is to play on the Frame now (no Scene targets it, or its Scene blacks it):
    dark on purpose, so Resting."""
    if facts.picture is None or facts.picture.showing or facts.wanted:
        return None
    return _Found(StatusWord.RESTING, Because(row=Row.NOTHING_WANTED))


def _not_on_screen(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """10: the Frame report says its picture is not on screen while one is to play, with no verdict yet."""
    if facts.picture is None or facts.picture.showing or not facts.wanted:
        return None
    return _Found(StatusWord.NOT_SHOWING, Because(row=Row.NOT_ON_SCREEN), trouble=_Trouble(
        Part.PHOTO_APP, ProblemCode.PICTURE_NOT_ON_SCREEN,
        Evidence(layer=EvidenceLayer.FRAME_REPORT, at=facts.picture.reported_at),
        fix=_cause_fix(CauseFix.RESTART_APP, facts)))


def _planning_trouble(facts: FrameFacts, issue: PlanningTrouble) -> _Trouble:
    source = issue.code in SOURCE_PLANNING_CODES
    return _Trouble(Part.IMMICH if source else Part.CENTRAL,
                    ProblemCode.SOURCE_TROUBLE if source else ProblemCode.PLANNING_ISSUE,
                    Evidence(layer=EvidenceLayer.PLAN, detail=issue.code, at=issue.since),
                    fix=_open_photo_source(facts, issue.scene_id), doing=DoingCode.KEEPS_PLAYING, since=issue.since)


def _trouble_while_showing(facts: FrameFacts, now: float) -> _Trouble | None:
    """Row 11's causes, the first that holds: no photo could be placed, a planning issue, a source's
    connection needs a look, its Pi's health, Central's continuation played past the limit."""
    if facts.unfilled:
        return _Trouble(Part.CENTRAL, ProblemCode.NO_PHOTO_PLACED,
                        Evidence(layer=EvidenceLayer.PLAN, at=min(span.start for span in facts.unfilled)),
                        fix=_open_photo_source(facts), doing=DoingCode.KEEPS_PLAYING)
    if facts.planning:
        return _planning_trouble(facts, facts.planning[0])
    if facts.sources:
        source = facts.sources[0]
        return _Trouble(Part.IMMICH, ProblemCode.LIBRARY_NEEDS_A_LOOK,
                        Evidence(layer=EvidenceLayer.LIBRARY, detail=source.problem, at=source.checked_at),
                        fix=CheckNow(connection_ref=source.connection_ref), doing=DoingCode.KEEPS_PLAYING,
                        since=source.since)
    health = facts.pi_health
    if health is not None:
        code = (ProblemCode.PI_THROTTLED if health.throttled else ProblemCode.PI_STORAGE_LOW if health.storage_low
                else ProblemCode.PI_CLOCK_IN_DOUBT if health.clock_in_doubt else None)
        if code is not None:
            return _Trouble(Part.PI, code, Evidence(layer=EvidenceLayer.HOST, at=health.at), fix=_open_pi(facts),
                            doing=DoingCode.KEEPS_PLAYING)
    continuing = None if facts.picture is None else facts.picture.continuing_since
    if continuing is not None and now - continuing > CONTINUATION_LIMIT_SECONDS:
        return _Trouble(Part.CENTRAL, ProblemCode.CONTINUING_PAST_LIMIT,
                        Evidence(layer=EvidenceLayer.FRAME_REPORT, at=facts.picture.reported_at),
                        fix=_open(OpenPage.CENTRAL, None), doing=DoingCode.KEEPS_PLAYING, since=continuing)
    return None


def _showing_with_trouble(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """11: showing, and something needs a look."""
    if facts.picture is None or not facts.picture.showing:
        return None
    trouble = _trouble_while_showing(facts, now)
    if trouble is None:
        return None
    return _Found(StatusWord.NEEDS_A_LOOK, Because(row=Row.SHOWING_WITH_TROUBLE), trouble=trouble, began=trouble.since)


def _showing(facts: FrameFacts, house: HouseFacts, now: float) -> _Found | None:
    """12: showing for Central's generation. A paused Frame is here (finding F3)."""
    if facts.picture is None or not facts.picture.showing:
        return None
    return _Found(StatusWord.SHOWING, Because(row=Row.SHOWING))


def _nothing_known(facts: FrameFacts, house: HouseFacts, now: float) -> _Found:
    """13: nothing above."""
    return _Found(StatusWord.CANT_TELL, Because(row=Row.NOTHING_KNOWN), began=facts.reports_at, trouble=_Trouble(
        Part.PI, ProblemCode.NO_REPORT, Evidence(layer=EvidenceLayer.FRAME_REPORT, at=facts.reports_at),
        fix=_open_pi(facts), since=facts.reports_at))


# The decision table: first match wins. The last row always matches.
ROWS: Final[tuple[tuple[Row, _RowRule], ...]] = (
    (Row.SETUP, _setup),
    (Row.CENTRAL_CANT_HEAR, _central_cant_hear),
    (Row.ON_THE_WAY, _on_the_way),
    (Row.PI_NOT_HEARD, _pi_not_heard),
    (Row.RESTING, _resting),
    (Row.WONT_SHOW, _wont_show),
    (Row.DISPLAY_STAYED_ON, _display_stayed_on),
    (Row.DARK, _dark),
    (Row.CENTRAL_BLACKS, _central_blacks),
    (Row.NOTHING_WANTED, _nothing_wanted),
    (Row.NOT_ON_SCREEN, _not_on_screen),
    (Row.SHOWING_WITH_TROUBLE, _showing_with_trouble),
    (Row.SHOWING, _showing),
    (Row.NOTHING_KNOWN, _nothing_known),
)


def actions(facts: FrameFacts, house: HouseFacts, setup: SetupStep | None) -> tuple[Action, ...]:
    """What may be asked of the Frame now, sorted: nothing while it is in setup; its Pi's reboot
    and its app's restart while Central's hub holds its Pi's link (each is a call over that link)."""
    hearing = facts.hearing
    if (setup is not None or facts.device_id is None or house.hub.state != "reachable"
            or hearing is None or hearing.linked is not True):
        return ()
    return tuple(sorted((Action.REBOOT_PI, Action.RESTART_APP)))


def frame_status(facts: FrameFacts, house: HouseFacts, now: float, kept: KeptStatus | None = None) -> FrameStatus:
    """The Frame's status at Central's `now`: the first matching row of `ROWS`. `kept` is its kept
    row (None: none): while the composed key equals its key, its `since` stands (the "since when"
    rule above)."""
    found = next(found for _, rule in ROWS if (found := rule(facts, house, now)) is not None)
    code = None if found.trouble is None else found.trouble.code
    key = key_of(found.word, found.setup, found.because, code)
    since = kept.since if kept is not None and kept.key == key else found.began if found.began is not None else now
    can = actions(facts, house, found.setup)
    problem = None
    if found.trouble is not None:
        trouble = found.trouble
        fix = trouble.fix
        # A card never offers what the Frame's menu may not: a fix asked of the Pi that is not in
        # `can` now (no hub, or its link not known) is a link to the Pi instead.
        if isinstance(fix, RestartApp | RebootPi) and Action(fix.kind) not in can:
            fix = _open_pi(facts)
        problem = Problem(part=trouble.part, code=trouble.code, doing=trouble.doing, fix=fix,
                          since=since if trouble.since is None else trouble.since, evidence=trouble.evidence,
                          frame_id=facts.frame_id)
    return FrameStatus(frame_id=facts.frame_id, word=found.word, setup=found.setup, since=since, because=found.because,
                       problem=problem, progress=found.progress, override=facts.override, can=can)


# --- The house ----------------------------------------------------------------------------------------

# The word each house problem speaks for the house.
HOUSE_PROBLEM_WORDS: Final[Mapping[ProblemCode, StatusWord]] = MappingProxyType({
    ProblemCode.HUB_UNREACHABLE: StatusWord.CANT_TELL, ProblemCode.WORKER_STOPPED: StatusWord.CANT_TELL,
    ProblemCode.NO_LIBRARY_ANSWERS: StatusWord.NEEDS_A_LOOK, ProblemCode.SCHEDULER_FAILING: StatusWord.NEEDS_A_LOOK,
    ProblemCode.PLANNING_ISSUE: StatusWord.NEEDS_A_LOOK, ProblemCode.SOURCE_TROUBLE: StatusWord.NEEDS_A_LOOK})


def _rank(word: StatusWord) -> int:
    return WORST_FIRST.index(word)


def _earliest(times: Sequence[float | None], now: float) -> float:
    return min((at for at in times if at is not None), default=now)


def house_problems(house: HouseFacts, frames: Sequence[FrameStatus], now: float) -> tuple[Problem, ...]:
    """The house's own problems, each with the Frames whose problems it explains: the hub (row 2);
    no library connection answers (every connection needs a look); Central's scheduler failing;
    each planning issue that names no Frame."""
    found: list[Problem] = []
    hub = _HUB_PROBLEMS.get(house.hub.state)
    if hub is not None:
        explained = [each for each in frames if each.because.row is Row.CENTRAL_CANT_HEAR]
        found.append(Problem(
            part=Part.CENTRAL, code=hub, doing=DoingCode.RECONNECTING, fix=_open(OpenPage.CENTRAL, None),
            since=_earliest([house.hub.since, *(each.since for each in explained)], now),
            evidence=Evidence(layer=EvidenceLayer.HUB, detail=house.hub.state, at=house.hub.since),
            explains=tuple(each.frame_id for each in explained)))
    if house.library_count > 0 and len(house.libraries) == house.library_count:
        first = min(house.libraries, key=lambda each: (each.since, each.connection_ref))
        found.append(Problem(
            part=Part.IMMICH, code=ProblemCode.NO_LIBRARY_ANSWERS, doing=DoingCode.KEEPS_PLAYING,
            fix=_open(OpenPage.LIBRARY, None), since=first.since,
            evidence=Evidence(layer=EvidenceLayer.LIBRARY, detail=first.problem, at=first.checked_at),
            explains=tuple(each.frame_id for each in frames
                           if each.problem is not None and each.problem.code is ProblemCode.LIBRARY_NEEDS_A_LOOK)))
    if house.failing_steps:
        steps = sorted(house.failing_steps, key=lambda each: each.step)
        found.append(Problem(
            part=Part.CENTRAL, code=ProblemCode.SCHEDULER_FAILING, fix=_open(OpenPage.CENTRAL, None),
            since=_earliest([each.since for each in steps], now),
            evidence=Evidence(layer=EvidenceLayer.SCHEDULER, detail=",".join(each.step for each in steps)[:128])))
    for code in sorted({each.code for each in house.planning}):
        issues = [each for each in house.planning if each.code == code]
        source = code in SOURCE_PLANNING_CODES
        scene = next((each.scene_id for each in issues if each.scene_id is not None), None)
        found.append(Problem(
            part=Part.IMMICH if source else Part.CENTRAL,
            code=ProblemCode.SOURCE_TROUBLE if source else ProblemCode.PLANNING_ISSUE,
            fix=_open(OpenPage.CENTRAL, None) if scene is None else _open(OpenPage.SCENE, scene),
            since=_earliest([each.since for each in issues], now),
            evidence=Evidence(layer=EvidenceLayer.PLAN, detail=code[:128], at=issues[0].since)))
    return tuple(found)


def house_status(frames: Sequence[FrameStatus], house: HouseFacts, now: float) -> HouseStatus:
    """The house word and Home's problems. The word is the worst of the Frames' words and the house
    problems' by the design language's order, All good when each is Showing or Resting; a Frame in
    setup does not count. A house problem replaces the Frame problems it explains: the hub going
    down is one problem, not one per Frame. The problems are worst first by the word each speaks
    (a house problem's, or its Frame's), a house problem before a Frame's of the same word."""
    own = house_problems(house, frames, now)
    explained = {frame_id for problem in own for frame_id in problem.explains}
    words = [each.word for each in frames if each.word is not None]
    words.extend(HOUSE_PROBLEM_WORDS[problem.code] for problem in own)
    worst = min(words, key=_rank, default=None)
    word: HouseWord = ALL_GOOD if worst is None or worst in GOOD else worst
    ranked = [((_rank(HOUSE_PROBLEM_WORDS[problem.code]), 0, problem.since, ""), problem) for problem in own]
    ranked.extend(((_rank(each.word), 1, each.problem.since, each.frame_id), each.problem) for each in frames
                  if each.problem is not None and each.word is not None and each.frame_id not in explained)
    return HouseStatus(word=word, problems=tuple(problem for _, problem in sorted(ranked, key=lambda each: each[0])))


__all__ = ["CAUSES", "CONTINUATION_LIMIT_SECONDS", "DISPLAY_PROBLEMS", "HOUSE_PROBLEM_WORDS", "REBOOT_ALLOWED_SECONDS",
           "ROWS", "SOURCE_PLANNING_CODES", "UNKNOWN_CAUSE", "Cause", "CauseFix", "actions", "frame_status",
           "house_problems", "house_status", "installing", "preparing", "rebooting"]
