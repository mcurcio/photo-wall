"""What a Frame's status is composed from (decision 0036, "The inputs"): one port per input, each
answering in its owner's own vocabulary or in plain facts, and `FrameStatusLog`. Pure.

None is new knowledge; the owner of each stays its judge. central/infra/frame_status_store.py
implements every port over PostgreSQL through that owner's own read, all in one transaction.
Every instant is on Central's clock, the one clock the policy compares with (the store places a
time its owner stamped by another clock there by its age), unless a field says otherwise.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from central.central_errors.views import FrameImpediment
from central.displays.model import Readiness
from central.frame_status.model import Because, Override, ProblemCode, Row, SetupStep, StatusWord
from central.kernel.transactions import Transaction
from central.power.model import PowerBecause
from contracts.node_output import Power, PowerResult

# The hub's state as its owner judges it (central/fleet/node_bus_presence.py `HubState`).
HubState = Literal["no_hub", "worker_stopped", "unreachable", "reachable"]
InstallStep = Literal["downloading", "restarting"]   # contracts/central_api.py `InstallProgress`


@dataclass(frozen=True, slots=True)
class DisplayPower:
    """Should the display be on, why, and did the Pi carry it out (central/power/timetable.py,
    central/why/explain.py `power_why`). `resting` is the Resting rule's answer (`is_resting`);
    `result` the Pi's last attempt's (None: not heard for the latest change, or no attempt needed);
    `due_on_at` when the display is next due on (a live test's end, else the timetable's next
    change to on; None: none known, a hold until resumed); `reported_at` when Central recorded the
    Pi's report (None: not kept)."""
    wanted: Power
    because: PowerBecause
    resting: bool
    result: PowerResult | None
    due_on_at: float | None
    reported_at: float | None = None


@dataclass(frozen=True, slots=True)
class Hub:
    """Central's hub: its state, and since when where its owner keeps a time (None: none kept)."""
    state: HubState
    since: float | None = None


@dataclass(frozen=True, slots=True)
class PiHearing:
    """Can Central hear the Frame's Pi (central/fleet/node_bus_presence.py,
    central/infra/player_contact_store.py): whether Central's hub holds its leaf (None: never
    looked) and since when (the worker's clock), when Central last heard from its Player (None:
    never on its current enrolment), and when it enrolled."""
    linked: bool | None
    linked_since: float | None
    heard_at: float | None
    enrolled_at: float


@dataclass(frozen=True, slots=True)
class Dark:
    """What the Pi says about the Frame's screen (central/fleet/frame_dark.py): its card's
    catalogue code (contracts/node_faults.py) and since when on the Pi's own clock (a set time,
    never compared with Central's)."""
    code: str
    pi_since: str


@dataclass(frozen=True, slots=True)
class Picture:
    """Is its picture on screen: the Player line's FrameReport for Central's generation of the
    Frame on its Output. `continuing_since` is show time (decision 0032); `reported_at` when
    Central recorded the report."""
    showing: bool
    continuing_since: float | None
    reported_at: float


@dataclass(frozen=True, slots=True)
class Reboot:
    """The Pi's newest reboot (central/fleet/pi_reboot.py): when it was asked (None: an Install's),
    its outcome's time (None: not run yet) and whether the host accepted it."""
    asked_at: float | None
    answered_at: float | None
    delivered: bool | None


@dataclass(frozen=True, slots=True)
class OnTheWay:
    """Is something on the way for its Pi: an install's step (central/infra/player_versions.py) and
    when that step began (Central's clock; None: not known), a reboot, and when Central recorded
    the host's current birth (its last start; None: none)."""
    install: InstallStep | None = None
    install_since: float | None = None
    reboot: Reboot | None = None
    host_born_at: float | None = None


@dataclass(frozen=True, slots=True)
class Preparation:
    """The Frame's photos in its Player's newest plan (central/coordination.py `frame_plans_in`):
    how many are planned, and how many the Player says it prepared or could not."""
    planned: int
    prepared: int
    failed: int


@dataclass(frozen=True, slots=True)
class PlanningTrouble:
    """One planning issue on the Frame (central/planning_outcome.py `PlanningIssue`), its code the
    Planner's own; `since` when the issue set it belongs to began."""
    code: str
    scene_id: str | None
    since: float | None


@dataclass(frozen=True, slots=True)
class UnfilledNow:
    """An unfilled span the Frame's plan holds, not yet over (contracts/models.py `UnfilledSpan`)."""
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class SourceTrouble:
    """A library connection that needs a look (central/photo_library/model.py `NeedsALook`): which
    problem (its `LibraryProblem` kind), since when, and when it was last checked."""
    connection_ref: str
    problem: str
    since: float
    checked_at: float


@dataclass(frozen=True, slots=True)
class PiHealth:
    """Its Pi's own health (central/fleet/node_observations.py): throttled or under-voltage now by
    the host's own bands, storage low, the wall clock in doubt by the kernel's verdict; `at` when
    Central recorded the newest of them."""
    throttled: bool = False
    storage_low: bool = False
    clock_in_doubt: bool = False
    at: float | None = None


@dataclass(frozen=True, slots=True)
class FrameFacts:
    """Everything one Frame's status is composed from, read in one transaction."""
    frame_id: str
    readiness: Readiness
    device_id: str | None = None                 # its Pi; None: unbound, or its Player has no device
    power: DisplayPower | None = None            # None: no Output
    hearing: PiHearing | None = None             # None: no Player
    dark: Dark | None = None
    picture: Picture | None = None               # None: no report for Central's generation
    reports_at: float | None = None              # when its Player last stated its Frame reports at all
    impediment: FrameImpediment | None = None    # None: not asked (no Output)
    on_the_way: OnTheWay = OnTheWay()
    preparation: Preparation | None = None
    planning: tuple[PlanningTrouble, ...] = ()
    unfilled: tuple[UnfilledNow, ...] = ()
    sources: tuple[SourceTrouble, ...] = ()
    pi_health: PiHealth | None = None
    override: Override | None = None
    scene_id: str | None = None                  # the Scene playing on it now (a fix's link)
    wanted: bool = False                         # a picture is to play on it now (the Runtime's Intent)


@dataclass(frozen=True, slots=True)
class FailingStep:
    """A scheduler step failing now (central/scheduler.py `StepHealth`) and since when."""
    step: str
    since: float | None


@dataclass(frozen=True, slots=True)
class HouseFacts:
    """What is true of the whole house: the hub, every library connection that needs a look and how
    many connections there are, the scheduler's failing steps, the planning issues that name no
    Frame."""
    hub: Hub = Hub("no_hub")
    libraries: tuple[SourceTrouble, ...] = ()
    library_count: int = 0
    failing_steps: tuple[FailingStep, ...] = ()
    planning: tuple[PlanningTrouble, ...] = ()


# --- The ports: one per input ---------------------------------------------------------------------
#
# Each is asked of one reading: one transaction's view of the wall at one `now`
# (`StatusReading`), so every answer agrees with the others. A mapping is by Frame id unless its
# port says "by device id"; a Frame or Pi with nothing to say is absent.

class SetupFacts(Protocol):
    def readiness(self) -> Mapping[str, Readiness]:
        """Is each Frame set up (central/displays/model.py `readiness`): every Frame."""
        ...

    def pis(self) -> Mapping[str, str]:
        """Each bound Frame's Pi, its device id."""
        ...


class PowerFacts(Protocol):
    def power(self) -> Mapping[str, DisplayPower]:
        """Should each bound Frame's display be on, why, and did the Pi carry it out."""
        ...


class HearingFacts(Protocol):
    def hub(self) -> Hub:
        """Central's hub's state."""
        ...

    def hearing(self) -> Mapping[str, PiHearing]:
        """Can Central hear each bound Frame's Pi."""
        ...


class ScreenFacts(Protocol):
    def dark(self) -> Mapping[str, Dark]:
        """Each Frame whose Pi's verdict says its screen is dark."""
        ...


class PictureFacts(Protocol):
    def pictures(self) -> Mapping[str, Picture]:
        """Each Frame's report of its picture, for Central's generation of it on its Output."""
        ...

    def reports_at(self) -> Mapping[str, float]:
        """When each bound Frame's Player last stated its Frame reports at all."""
        ...


class ImpedimentFacts(Protocol):
    def impediments(self) -> Mapping[str, FrameImpediment]:
        """Does Central black each bound Frame (central/central_errors/compose.py `impediment`)."""
        ...


class OnTheWayFacts(Protocol):
    def on_the_way(self) -> Mapping[str, OnTheWay]:
        """Is something on the way for each Pi, by device id."""
        ...

    def preparation(self) -> Mapping[str, Preparation]:
        """Each Frame's photos in its Player's newest plan."""
        ...


class PlanningFacts(Protocol):
    def planning(self) -> Mapping[str | None, tuple[PlanningTrouble, ...]]:
        """The last planning pass's issues by the Frame each names; under None, those naming none."""
        ...

    def unfilled(self) -> Mapping[str, tuple[UnfilledNow, ...]]:
        """Each Frame's unfilled spans not yet over."""
        ...


class SourceFacts(Protocol):
    def playing_scenes(self) -> Mapping[str, str]:
        """The Scene playing on each Frame now."""
        ...

    def wanted(self) -> frozenset[str]:
        """The Frames a picture is to play on now: each whose visible Intent is media (central/runtime.py
        `RuntimeView.for_target`). A Frame no Scene targets now, or one a Scene blacks, is absent:
        it is dark on purpose."""
        ...

    def sources(self) -> Mapping[str, tuple[SourceTrouble, ...]]:
        """For each Frame, the connections its playing Scene reads that need a look."""
        ...

    def libraries(self) -> tuple[tuple[SourceTrouble, ...], int]:
        """Every connection that needs a look, and how many connections there are."""
        ...


class PiHealthFacts(Protocol):
    def pi_health(self) -> Mapping[str, PiHealth]:
        """Each Pi's own health, by device id."""
        ...


class OverrideFacts(Protocol):
    def overrides(self) -> tuple[Override, ...]:
        """Every hold, pause and takeover in force, each with its Frames; a Frame's own is the
        first here that names it."""
        ...


class SchedulerSteps(Protocol):
    def failing_steps(self) -> tuple[FailingStep, ...]:
        """The scheduler's steps failing now."""
        ...


class StatusReading(SetupFacts, PowerFacts, HearingFacts, ScreenFacts, PictureFacts, ImpedimentFacts, OnTheWayFacts,
                    PlanningFacts, SourceFacts, PiHealthFacts, OverrideFacts, SchedulerSteps, Protocol):
    """Every input, read in one transaction at one instant."""

    @property
    def now(self) -> float:
        """Central's clock at this reading."""
        ...


def facts_of(reading: StatusReading) -> tuple[dict[str, FrameFacts], HouseFacts]:
    """Every Frame's facts and the house's, gathered from one reading. Pure: it only places each
    port's answer on its Frame (a Pi's answers on each Frame the Pi feeds)."""
    pis, power, hearing, dark = reading.pis(), reading.power(), reading.hearing(), reading.dark()
    pictures, reports_at, impediments = reading.pictures(), reading.reports_at(), reading.impediments()
    on_the_way, preparation, planning = reading.on_the_way(), reading.preparation(), reading.planning()
    unfilled, scenes, sources, pi_health = reading.unfilled(), reading.playing_scenes(), reading.sources(), reading.pi_health()
    overrides, wanted = reading.overrides(), reading.wanted()
    libraries, library_count = reading.libraries()
    frames = {}
    for frame_id, readiness in reading.readiness().items():
        device_id = pis.get(frame_id)
        frames[frame_id] = FrameFacts(
            frame_id=frame_id, readiness=readiness, device_id=device_id, power=power.get(frame_id),
            hearing=hearing.get(frame_id), dark=dark.get(frame_id), picture=pictures.get(frame_id),
            reports_at=reports_at.get(frame_id), impediment=impediments.get(frame_id),
            on_the_way=on_the_way.get(device_id, OnTheWay()) if device_id is not None else OnTheWay(),
            preparation=preparation.get(frame_id), planning=planning.get(frame_id, ()),
            unfilled=unfilled.get(frame_id, ()), sources=sources.get(frame_id, ()),
            pi_health=None if device_id is None else pi_health.get(device_id),
            override=next((each for each in overrides if frame_id in each.frame_ids), None),
            scene_id=scenes.get(frame_id), wanted=frame_id in wanted)
    house = HouseFacts(hub=reading.hub(), libraries=libraries, library_count=library_count,
                       failing_steps=reading.failing_steps(), planning=planning.get(None, ()))
    return frames, house


@dataclass(frozen=True, slots=True)
class StatusKey:
    """What of a Frame's status is kept: a change of any of these is a change of its status."""
    word: StatusWord | None
    setup: SetupStep | None
    row: Row
    problem: ProblemCode | None


@dataclass(frozen=True, slots=True)
class KeptStatus:
    """A Frame's kept row: its key and since when it has stood."""
    key: StatusKey
    since: float


@dataclass(frozen=True, slots=True)
class StatusChange:
    """One change of a Frame's status as the event log records it."""
    frame_id: str
    key: StatusKey
    since: float
    recorded_at: float


class FrameStatusLog(Protocol):
    """The `frame_status` table and its records in Central's event log, in the caller's transaction."""

    def kept_in(self, tx: Transaction) -> Mapping[str, KeptStatus]:
        """Every Frame's kept row, by Frame id."""
        ...

    def record_in(self, tx: Transaction, frame_id: str, key: StatusKey, since: float) -> None:
        """Keep `frame_id`'s status as `key` since `since`, and append one record of the change to
        the event log. Called only for a Frame whose key differs from its kept row's."""
        ...

    def recent_in(self, tx: Transaction, frame_id: str, limit: int) -> tuple[StatusChange, ...]:
        """`frame_id`'s newest `limit` recorded changes, newest first."""
        ...


def key_of(word: StatusWord | None, setup: SetupStep | None, because: Because, problem: ProblemCode | None) -> StatusKey:
    """The kept key of a composed status."""
    return StatusKey(word=word, setup=setup, row=because.row, problem=problem)


__all__ = ["Dark", "DisplayPower", "FailingStep", "FrameFacts", "FrameStatusLog", "HearingFacts", "HouseFacts", "Hub",
           "HubState", "ImpedimentFacts", "InstallStep", "KeptStatus", "OnTheWay", "OnTheWayFacts", "OverrideFacts",
           "PiHealth", "PiHealthFacts", "PiHearing", "Picture", "PictureFacts", "PlanningFacts", "PlanningTrouble",
           "PowerFacts", "Preparation", "Reboot", "SchedulerSteps", "ScreenFacts", "SetupFacts", "SourceFacts",
           "SourceTrouble", "StatusChange", "StatusKey", "StatusReading", "UnfilledNow", "facts_of", "key_of"]
