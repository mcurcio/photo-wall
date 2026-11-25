"""A Frame's status: the vocabulary (decision 0036, "The shapes"). Pure.

Central sends codes; every string a person reads is the console's, worded from the generated
contract (central/console_contract.py). Each vocabulary here is closed, so a code the console does
not word fails its typecheck. Instants are Central's clock (seconds since the epoch).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field

from central.everyday.model import Asked
from central.power.model import PowerBecause
from contracts.identifiers import Identifier
from contracts.models import Instant, Model
from contracts.node_output import Power


class StatusWord(StrEnum):
    """The six status words (design language §6), their one home in Central."""
    NOT_SHOWING = "not-showing"
    CANT_TELL = "cant-tell"
    NEEDS_A_LOOK = "needs-a-look"
    GETTING_READY = "getting-ready"
    SHOWING = "showing"
    RESTING = "resting"


# The design language's order, worst first; Showing and Resting are equally good.
WORST_FIRST: Final[tuple[StatusWord, ...]] = (
    StatusWord.NOT_SHOWING, StatusWord.CANT_TELL, StatusWord.NEEDS_A_LOOK, StatusWord.GETTING_READY,
    StatusWord.SHOWING, StatusWord.RESTING)
GOOD: Final[frozenset[StatusWord]] = frozenset({StatusWord.SHOWING, StatusWord.RESTING})
ALL_GOOD: Final = "all-good"
HouseWord = StatusWord | Literal["all-good"]


class SetupStep(StrEnum):
    """Setup is not status: the step a Frame's setup waits on."""
    NEEDS_A_PI = "needs-a-pi"
    DISPLAY_CHANGED = "display-changed"
    CHECK_POSITION = "check-position"


class Row(StrEnum):
    """The decision table's rows (policy.py `ROWS`), in its order."""
    SETUP = "setup"                              # 1
    CENTRAL_CANT_HEAR = "central-cant-hear"      # 2
    ON_THE_WAY = "on-the-way"                    # 3
    PI_NOT_HEARD = "pi-not-heard"                # 4
    RESTING = "resting"                          # 5
    WONT_SHOW = "wont-show"                      # 6
    DISPLAY_STAYED_ON = "display-stayed-on"      # 7
    DARK = "dark"                                # 8
    CENTRAL_BLACKS = "central-blacks"            # 9
    NOTHING_WANTED = "nothing-wanted"            # 10 as built: black, and nothing is to play
    NOT_ON_SCREEN = "not-on-screen"              # 10
    SHOWING_WITH_TROUBLE = "showing-with-trouble"  # 11
    SHOWING = "showing"                          # 12
    NOTHING_KNOWN = "nothing-known"              # 13


class Because(Model):
    """The one row of the decision table that matched. `power`: why the display is wanted off
    (rows 5 to 7); `until`: when that ends by itself, the display's next due on ("Won't show at
    07:00"), None when no time is known (a hold until resumed)."""
    row: Row
    power: PowerBecause | None = None
    until: Instant | None = None


class Part(StrEnum):
    """The six parts a problem can name (0036's values, verbatim)."""
    DISPLAY = "display"
    PI = "pi"
    PHOTO_APP = "photo app"
    NETWORK = "network"
    CENTRAL = "central"
    IMMICH = "immich"


class ProblemCode(StrEnum):
    """What is wrong, closed; the console words each."""
    # central
    HUB_UNREACHABLE = "hub-unreachable"
    WORKER_STOPPED = "worker-stopped"
    SCHEDULER_FAILING = "scheduler-failing"
    NO_INSTRUCTION = "no-instruction"            # the Pi has no instruction for this screen
    CENTRAL_BLACKS = "central-blacks"            # Central's error blacks the Frame
    NO_PHOTO_PLACED = "no-photo-placed"          # an unfilled span: a photo was wanted, none placed
    PLANNING_ISSUE = "planning-issue"
    CONTINUING_PAST_LIMIT = "continuing-past-limit"
    # pi
    PI_NOT_LINKED = "pi-not-linked"
    PI_NOT_HEARD = "pi-not-heard"
    NO_REPORT = "no-report"
    CANNOT_DRAW = "cannot-draw"
    SOFTWARE_RENDERER = "software-renderer"
    PI_CLOCK = "pi-clock"                        # the Pi has no time from Central
    PI_THROTTLED = "pi-throttled"
    PI_STORAGE_LOW = "pi-storage-low"
    PI_CLOCK_IN_DOUBT = "pi-clock-in-doubt"
    DARK_UNKNOWN = "dark-unknown"                # a dark code this Central's cause table lacks
    # photo app
    APP_ABSENT = "app-absent"
    APP_UNRESPONSIVE = "app-unresponsive"
    APP_RESOURCE_EXHAUSTED = "app-resource-exhausted"
    APP_DESCRIPTOR_LIMIT = "app-descriptor-limit"
    PICTURE_NOT_ON_SCREEN = "picture-not-on-screen"
    # display
    DISPLAY_DID_NOT_ANSWER = "display-did-not-answer"
    DISPLAY_ANOTHER_INPUT = "display-another-input"
    # immich
    NO_PHOTOS = "no-photos"                      # no photo for the Frame, and none being prepared
    SOURCE_TROUBLE = "source-trouble"            # planning could not read a Scene's Photo source
    LIBRARY_NEEDS_A_LOOK = "library-needs-a-look"
    NO_LIBRARY_ANSWERS = "no-library-answers"


class DoingCode(StrEnum):
    """What Photo Wall is doing about a problem, closed."""
    RECONNECTING = "reconnecting"                # Central keeps trying to reach it
    KEEPS_PLAYING = "keeps-playing"              # the Frame keeps playing what it has


class OpenPage(StrEnum):
    """The pages an `open` fix can lead to: each owns the setting to change."""
    FRAME = "frame"
    PI = "pi"
    SCENE = "scene"                              # the Scene's Photo source
    LIBRARY = "library"                          # Settings › Photo library
    CENTRAL = "central"                          # Central's own health


class RestartApp(Model):
    kind: Literal["restart-app"] = "restart-app"
    device_id: Identifier


class RebootPi(Model):
    kind: Literal["reboot-pi"] = "reboot-pi"
    device_id: Identifier


class ResumeSchedule(Model):
    kind: Literal["resume-schedule"] = "resume-schedule"
    hold_id: Identifier


class CarryOn(Model):
    kind: Literal["carry-on"] = "carry-on"
    pause_id: Identifier


class CheckNow(Model):
    kind: Literal["check-now"] = "check-now"
    connection_ref: Identifier


class Open(Model):
    """A link to the page that owns the setting; `id` names the Frame, Pi (device), Scene or
    connection, None for a page with no object."""
    kind: Literal["open"] = "open"
    page: OpenPage
    id: Identifier | None = None


# Closed: a fix the system cannot perform cannot be expressed.
Fix = Annotated[RestartApp | RebootPi | ResumeSchedule | CarryOn | CheckNow | Open,
                Field(discriminator="kind", title="Fix")]


class EvidenceLayer(StrEnum):
    """Where a problem's evidence came from (Details)."""
    HUB = "hub"
    PRESENCE = "presence"
    HEALTH_VERDICT = "health-verdict"
    FRAME_REPORT = "frame-report"
    POWER_REPORT = "power-report"
    IMPEDIMENT = "impediment"
    PLAN = "plan"
    LIBRARY = "library"
    HOST = "host"
    SCHEDULER = "scheduler"


class Evidence(Model):
    """The layer the evidence came from, its owner's own code for it (`detail`: a fault code, a
    power result, a planning code, a library problem kind, a scheduler step; shown in Details,
    never worded by Central), when Central recorded it (`at`), and the Pi's own time for it when
    the Pi stated one (`pi_time`, a set time: shown, never compared with Central's clock)."""
    layer: EvidenceLayer
    detail: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    at: Instant | None = None
    pi_time: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class Problem(Model):
    """One thing wrong: the part that failed, what, since when, what Photo Wall is doing about it
    and the one fix. `frame_id` is the Frame it is on; a house problem has none, and `explains`
    names the Frames whose own problems it replaces on Home."""
    part: Part
    code: ProblemCode
    since: Instant
    doing: DoingCode | None = None
    fix: Fix | None = None
    evidence: Evidence
    frame_id: Identifier | None = None
    explains: tuple[Identifier, ...] = ()


class ProgressStep(StrEnum):
    """What a Frame that is Getting ready waits on."""
    INSTALL_RESTARTING = "install-restarting"
    REBOOTING = "rebooting"
    STARTING = "starting"                        # enrolled, no report yet
    WAITING_TO_START = "waiting-to-start"        # the Pi waits for Central to start its photo
    PREPARING_PHOTOS = "preparing-photos"


class Progress(Model):
    """The step, and its count when the step has one (photos prepared of those planned)."""
    step: ProgressStep
    done: Annotated[int, Field(ge=0)] | None = None
    total: Annotated[int, Field(ge=1)] | None = None


class OverrideKind(StrEnum):
    POWER_HOLD = "power-hold"
    PAUSE = "pause"                              # slice 3: a Frame holding its photo (finding F3: Showing)
    TAKEOVER = "takeover"


class Override(Model):
    """A hold, a pause or a takeover in force: which (`id`: the hold's, the pause's or the Run's),
    on which Frames, by whom (an actor, central/actor.py; None: not recorded) and until when (None:
    until it is ended by hand). `power` is a hold's; `name` a takeover's Scene's."""
    kind: OverrideKind
    id: Identifier
    frame_ids: tuple[Identifier, ...]
    power: Power | None = None
    name: str | None = None
    by: str | None = None
    started_at: Instant | None = None
    until: Instant | None = None


class Action(StrEnum):
    """What may be asked of a Frame now (`FrameStatus.can`); an action not offered is absent,
    never disabled. Slice 3's everyday actions are offered per central/everyday/policy.py `stays`."""
    RESTART_APP = "restart-app"
    REBOOT_PI = "reboot-pi"
    SKIP = "skip"
    PREVIOUS = "previous"
    NEVER_SHOW = "never-show"
    PAUSE = "pause"


class FrameStatus(Model):
    """One Frame's status. `word` is None while the Frame is in setup (`setup` says which step);
    `since` is when this word began; `because` the row that matched; `problem` is present for Not
    showing, Can't tell and Needs a look; `progress` for Getting ready; `can` is sorted."""
    frame_id: Identifier
    word: StatusWord | None
    setup: SetupStep | None
    since: Instant
    because: Because
    problem: Problem | None = None
    progress: Progress | None = None
    override: Override | None = None
    can: tuple[Action, ...] = ()
    # Slice 3: the newest action asked of it that moved it, and whether its Player has shown it.
    asked: Asked | None = None


class HouseStatus(Model):
    """The house word and Home's problems, worst first: each house problem (no Frame), then each
    Frame's problem no house problem explains."""
    word: HouseWord
    problems: tuple[Problem, ...] = ()


__all__ = ["ALL_GOOD", "GOOD", "WORST_FIRST", "Action", "Because", "CarryOn", "CheckNow", "DoingCode", "Evidence",
           "EvidenceLayer", "Fix", "FrameStatus", "HouseStatus", "HouseWord", "Open", "OpenPage", "Override",
           "OverrideKind", "Part", "Problem", "ProblemCode", "Progress", "ProgressStep", "RebootPi", "RestartApp",
           "ResumeSchedule", "Row", "SetupStep", "StatusWord"]
