"""Deterministic Scene lifecycle and intent, without I/O or media selection.

See docs/module-runtime.md. UTC is always supplied by the caller. Persist an
export in the same transaction as activation ingress before publishing its intent.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from datetime import timedelta
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from central.house_time import calculator
from central.house_time.model import HouseTime, LocalDateTime, When, Window
from central.wall_plan.selection import SceneSelection
from contracts.models import TARGET_ID_PATTERN, AfterEnd

if TYPE_CHECKING:
    from central.execution_outcomes import ExecutionOutcome

Target = Annotated[str, Field(pattern=rf"^(frame|actuator):{TARGET_ID_PATTERN}$")]
Seconds = Annotated[FiniteFloat, Field(ge=0)]
PositiveSeconds = Annotated[FiniteFloat, Field(gt=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Unit = Annotated[FiniteFloat, Field(ge=0, le=1)]


def handle_execution_outcome(outcome: ExecutionOutcome) -> str:
    """Own the lifecycle consequence of a reported execution fact.

    Execution failures do not silently finish or cancel a logical Run. The
    coordinator revokes affected execution while Runtime continues its authored
    lifecycle. Observations are accepted as observations, not completion proof.
    """
    if outcome.kind == "observation":
        return "record_observation"
    return "preserve_lifecycle"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Contribution(FrozenModel):
    """Authored intent; references identify centrally owned versioned definitions."""

    target: Target
    kind: Literal["media", "black", "actuator"] = "media"
    role: Identifier | None = None
    source_refs: tuple[Identifier, ...] = ()
    asset_refs: tuple[Identifier, ...] = ()
    opacity: Unit = 1
    fade_in_seconds: Seconds = 0
    fade_out_seconds: Seconds = 0
    after_end: AfterEnd = "leave_as_is"
    ramp_from: Unit = 0
    ramp_to: Unit = 0

    @model_validator(mode="after")
    def consistent_kind(self) -> Self:
        if (self.kind == "actuator") != self.target.startswith("actuator:"):
            raise ValueError("actuator targets require actuator contributions and vice versa")
        if self.kind == "media" and not (self.source_refs or self.asset_refs):
            raise ValueError("media contributions need source or authored asset references")
        if self.kind != "media" and (self.source_refs or self.asset_refs):
            raise ValueError("only media contributions carry media references")
        if self.after_end == "keep_this_photo" and (self.kind != "media" or self.opacity != 1):
            raise ValueError("only opaque media contributions may keep a photo")
        if self.after_end != "leave_as_is" and self.kind == "actuator":
            raise ValueError("an actuator contribution keeps nothing on a Frame")
        return self


class Child(FrozenModel):
    scene: Scene
    delay_seconds: Seconds = 0


class Scene(FrozenModel):
    scene_id: Identifier
    revision: Annotated[int, Field(ge=1)] = 1
    contributions: tuple[Contribution, ...] = ()
    children: tuple[Child, ...] = ()
    cycle_seconds: PositiveSeconds = 30
    loop: bool = False
    duration_seconds: PositiveSeconds | None = None
    outro_seconds: Seconds = 0
    outro_contributions: tuple[Contribution, ...] = ()
    protect_frames: bool = False
    # Roadmap 5a: how this Scene's Frames pick from its photos (order, no-repeat days, crop limit).
    selection: SceneSelection = SceneSelection()

    @model_validator(mode="after")
    def coherent_cycle(self) -> Self:
        for contributions in (self.contributions, self.outro_contributions):
            if len({c.target for c in contributions}) != len(contributions):
                raise ValueError("a Scene phase may contribute only once to each target")
        if any(child.delay_seconds >= self.cycle_seconds for child in self.children):
            raise ValueError("children must start before the parent cycle ends")
        if self.outro_contributions and self.outro_seconds == 0:
            raise ValueError("outro contributions require a positive outro duration")
        return self

    @property
    def participants(self) -> frozenset[str]:
        return frozenset(
            [c.target for c in (*self.contributions, *self.outro_contributions)]
            + [target for child in self.children for target in child.scene.participants]
        )

    @property
    def protected_frames(self) -> frozenset[str]:
        if self.protect_frames:
            return frozenset(t for t in self.participants if t.startswith("frame:"))
        return frozenset(t for child in self.children for t in child.scene.protected_frames)


Child.model_rebuild()


def _unprotected(scene: Scene) -> Scene:
    """`scene` with no protection at any depth (its own `protect_frames` and every child's)."""
    children = tuple(child.model_copy(update={"scene": _unprotected(child.scene)}) for child in scene.children)
    return scene.model_copy(update={"protect_frames": False, "children": children})


# Power and the week (roadmap 2b; frozen page /Volumes/Dock/tmp/power-week/slices/T.md). Slice T
# reshapes `Program` below into a repeating rule: {program_id, name, scene_id, when, priority,
# revision}, its windows derived from `when` in house time, never stored. Content's arbiter
# (frame design rule 3): the highest rank wins, a tie to the request admitted last.
Priority = Literal["normal", "special-event", "always-wins"]          # #78; "normal" is the default
PRIORITY_RANK: Final[Mapping[str, int]] = MappingProxyType(
    {"normal": 1, "special-event": 2, "always-wins": 3})
# The Default Scene plays under every Schedule and every operator request (E5): its rank is below
# any priority `activate` accepts (0 and up), so it never wins a tie.
DEFAULT_SCENE_RANK: Final = -1


class ProgramDraft(FrozenModel):
    """What the Schedule flow saves (#72-#76, #78): a Scene, its weekly When and a priority. Slice T
    makes `Program` this plus `program_id` (Central's, assigned at create) and `revision` (1 at
    create, +1 per save)."""
    name: Annotated[str, Field(min_length=1, max_length=80)]
    scene_id: Identifier
    when: When
    priority: Priority = "normal"


class DefaultScene(FrozenModel):
    """The Scene that plays wherever no Schedule does (E5). `revision` rises at every change, so
    each choice's Runs have their own identity `default:<scene_id>:<revision>:<scene revision>:
    <start>` (`Runtime._default_prefix`): an edit of its Scene is a new identity too."""
    scene_id: Identifier
    revision: Annotated[int, Field(ge=1)]


class ProgramRefused(Exception):
    """A Program or Default Scene command refused; nothing changed. `code`: "changed" (the stored
    Program is not the expected one; `current` is it), "missing", "unknown-scene", "no-days",
    "needs-place". The routes answer {"error": code} (409 changed, 404 missing, 422 the rest),
    with "current" for "changed"."""

    def __init__(self, code: Literal["changed", "missing", "unknown-scene", "no-days", "needs-place"],
                 current: Program | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current = current


class Program(ProgramDraft):
    """A repeating Schedule (#72-#78): its windows come from `when` in house time, never stored.
    A window's activation identity is `window_id(start)`; `revision` is part of it, so a saved
    rule's windows never alias the previous rule's."""
    program_id: Identifier
    revision: Annotated[int, Field(ge=1)] = 1

    def window_id(self, start: float) -> str:
        return f"{self.window_prefix}{start:.17g}"

    @property
    def window_prefix(self) -> str:
        """What every window identity of this revision starts with."""
        return f"{PROGRAM_PREFIX}{self.program_id}:{self.revision}:"


DEFAULT_PREFIX: Final = "default:"
PROGRAM_PREFIX: Final = "program:"


class Admission(FrozenModel):
    activation_id: str
    status: Literal["admitted", "ignored", "queued", "rejected", "expired"]
    run_id: str | None = None
    reason: str | None = None
    # The root Run whose protection refused it (a protected refusal only).
    # Optional so admissions stored before it existed still restore.
    blocking_run_id: str | None = None


class Intent(FrozenModel):
    target: Target
    kind: Literal["media", "black", "actuator"]
    run_id: str
    root_id: str
    scene_id: str
    scene_revision: int
    role: str | None
    source_refs: tuple[str, ...]
    asset_refs: tuple[str, ...]
    priority: int
    root_order: int
    admission_order: int
    logical_origin: FiniteFloat
    logical_position: Seconds
    cycle_index: int
    cycle_position: Seconds
    interval_start: FiniteFloat
    interval_end: FiniteFloat
    opacity: Unit
    base_opacity: Unit
    fade_in_seconds: Seconds
    fade_out_seconds: Seconds
    after_end: AfterEnd = "leave_as_is"
    actuator_value: Unit | None = None
    phase: Literal["body", "outro"]
    # Roadmap 5a: the Intent's own Scene's selection (a child Scene's is its own), copied by
    # `timeline` (slice O1); the Planner reads it.
    selection: SceneSelection = SceneSelection()

    @property
    def precedence(self) -> tuple[int, int, int]:
        return (self.priority, self.root_order, self.admission_order)


class RunView(FrozenModel):
    run_id: str
    root_id: str
    parent_id: str | None
    scene_id: str
    scene_revision: int
    started_at: FiniteFloat
    phase: str
    participants: frozenset[str]
    children: tuple[str, ...]
    finish_requested_at: FiniteFloat | None
    ended_at: FiniteFloat | None
    program_id: str | None
    priority: int
    # The root Run's activation identity ("program:...", "default:..." or Show now's); None for a
    # child, and for a Run admitted before Power and the week.
    activation_id: str | None = None


class RuntimeView(FrozenModel):
    now: FiniteFloat
    runs: tuple[RunView, ...]
    contributions: tuple[Intent, ...]
    visible: tuple[Intent, ...]

    def for_target(self, target: str) -> Intent | None:
        return next((intent for intent in self.visible if intent.target == target), None)


# The operator's read keeps ended Runs for this long, and Runtime forgets a root Run (with its
# admission) once it and its Program window, if any, ended this long ago (`Runtime._forget`).
OPERATOR_HISTORY_SECONDS = 86400.0


class ProgramWindowView(FrozenModel):
    """One window of a Program, worked out in Central: UTC instants and the house clock's text."""
    start_at: FiniteFloat
    end_at: FiniteFloat
    start: LocalDateTime                             # house clock, display only
    end: LocalDateTime
    admission: Admission | None                      # this window's, None until it starts


class ProgramWindows(FrozenModel):
    current: ProgramWindowView | None                # start_at <= now < end_at
    next: ProgramWindowView | None                   # the first window starting after now that its
                                                     # current rule plays (at or after program_from)


class OperatorProjection(FrozenModel):
    """The operator console's read of the Runtime (GET /v1/operator/runtime).

    `current` is the projected view with its Runs bounded to the live ones plus
    those ended in the last `OPERATOR_HISTORY_SECONDS`. `protected_frames` maps
    each served Run that protects frames to those targets; it is computed only
    here, never on the scheduler's path. `program_windows` maps each Program to
    its current and next window (house time worked out here, never in the
    console); `default_scene` is the Default Scene. Runtime itself forgets what
    ended longer ago than the bound (`Runtime._forget`).
    """

    current: RuntimeView
    protected_frames: Mapping[str, frozenset[str]]
    program_windows: Mapping[str, ProgramWindows]
    default_scene: DefaultScene | None


class _MutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class _Run(_MutableModel):
    run_id: str
    root_id: str
    parent_id: str | None = None
    scene: Scene
    started_at: FiniteFloat
    priority: int
    order: int
    root_order: int
    local_order: int = 0
    descendant_sequence: int = 0
    program_id: str | None = None
    program_ends_at: FiniteFloat | None = None
    activation_id: str | None = None
    scheduled_finish_at: FiniteFloat | None = None
    phase: Literal["body", "outro", "completed", "cancelled"] = "body"
    cycle_index: int = 0
    launched_children: set[int] = Field(default_factory=set)
    children: list[str] = Field(default_factory=list)
    children_finished_at: FiniteFloat | None = None
    finish_requested_at: FiniteFloat | None = None
    body_done_at: FiniteFloat | None = None
    outro_started_at: FiniteFloat | None = None
    ended_at: FiniteFloat | None = None

    @property
    def cycle_start(self) -> float:
        return self.started_at + self.cycle_index * self.scene.cycle_seconds

    @property
    def cycle_end(self) -> float:
        return self.started_at + (self.cycle_index + 1) * self.scene.cycle_seconds

    @property
    def active(self) -> bool:
        return self.phase in ("body", "outro")

    @property
    def stop_at(self) -> float | None:
        times = [self.program_ends_at, self.scheduled_finish_at, self.finish_requested_at]
        if self.scene.duration_seconds is not None:
            times.append(self.started_at + self.scene.duration_seconds)
        return min((t for t in times if t is not None), default=None)


def _may_hold_to_the_end(run: _Run, contribution: Contribution) -> bool:
    """A kept photo in its Run's final body cycle, with no ending of its Scene on its Frame:
    it plans no fade-out unless another Run plays on the Frame at the cycle's end
    (`Runtime._playing_at`), so it never dips to black and snaps back to itself. The cycle is
    final when the Scene does not loop, or when the Run's stop (Program end, duration,
    scheduled finish or Finish) falls within it; a Finish, or a follower that appears later,
    revises the cycle's fade-out, which the planner allows on an offered layer
    (`planner.REVISABLE`)."""
    if run.phase != "body" or contribution.after_end != "keep_this_photo":
        return False
    if any(ending.target == contribution.target for ending in run.scene.outro_contributions):
        return False
    stop = run.stop_at
    return not run.scene.loop or (stop is not None and stop <= run.cycle_end)


class _Queued(_MutableModel):
    activation_id: str
    scene: Scene
    priority: int
    force: bool
    expires_at: FiniteFloat


class _State(_MutableModel):
    version: Literal[1] = 1
    now: FiniteFloat | None = None
    sequence: int = 0
    scenes: dict[str, Scene] = Field(default_factory=dict)
    programs: dict[str, Program] = Field(default_factory=dict)
    # A Program's windows are admitted only from this instant (its create, or the end of the
    # window playing at its save: R10).
    program_from: dict[str, FiniteFloat] = Field(default_factory=dict)
    default_scene: DefaultScene | None = None
    runs: dict[str, _Run] = Field(default_factory=dict)
    admissions: dict[str, Admission] = Field(default_factory=dict)
    queue: list[_Queued] = Field(default_factory=list)


# Windows are worked out per house day and reused: a lookup per event must not redo the calendar.
_WINDOW_DAYS: Final = calculator.LOOKAHEAD_DAYS + 2


@lru_cache(maxsize=512)
def _windows_from_day(when: When, house: HouseTime, day_start: float) -> tuple[Window, ...]:
    day = calculator.house_date(house, day_start)
    end = calculator.day_start(house, day + timedelta(days=_WINDOW_DAYS))
    try:
        return calculator.windows(when, house, day_start, end)
    except ValueError:      # a When the house can no longer place (its Place removed): no windows
        return ()


def _windows_after(when: When, house: HouseTime, floor: float) -> tuple[Window, ...]:
    """The windows of `when` ending after `floor`, looking LOOKAHEAD_DAYS house days ahead."""
    first = calculator.day_start(house, calculator.house_date(house, floor))
    return tuple(window for window in _windows_from_day(when, house, first) if window.end > floor)


def _finite(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("time must be a finite UTC number")
    return float(value)


def _run_id(identity: str) -> str:
    return "run-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _refusal(activation_id: str, denial: tuple[str, str]) -> Admission:
    reason, blocking_run_id = denial
    return Admission(
        activation_id=activation_id, status="rejected", reason=reason,
        blocking_run_id=blocking_run_id,
    )


class RuntimeConflict(Exception):
    """A command refused because it conflicts with the stored state; nothing changed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SceneInUse(RuntimeConflict):
    """A stored Scene definition is still referenced by durable runtime state."""

    def __init__(self, *, program_ids=(), run_ids=(), queued_activation_ids=(), scene_ids=(),
                 default_scene: bool = False) -> None:
        self.default_scene = default_scene
        self.program_ids = tuple(sorted(program_ids))
        self.run_ids = tuple(sorted(run_ids))
        self.queued_activation_ids = tuple(sorted(queued_activation_ids))
        self.scene_ids = tuple(sorted(scene_ids))
        super().__init__("scene_in_use")


class RuntimeBudgetExceeded(RuntimeError):
    """A projection/current advance exceeded its explicit transition budget."""


class _TransitionBudget:
    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("max_events must be a positive integer")
        self.remaining = limit

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise RuntimeBudgetExceeded("Runtime transition budget exceeded")


class Runtime:
    """Single-owner mutable domain state, with pure immutable observations. `house` turns each
    Program's When into windows (injected per load, never stored in the state)."""

    def __init__(self, *, house: HouseTime) -> None:
        if not isinstance(house, HouseTime):
            raise TypeError("house must be a HouseTime")
        self._state = _State()
        self._house = house
        self._target: float | None = None     # the instant an advance is moving to (transient)

    @property
    def house(self) -> HouseTime:
        return self._house

    def set_scene(self, scene: Scene) -> None:
        """Store a Scene; a save must move its revision past the stored one.

        A revision at or below the stored one is refused (`scene_revision_conflict`)
        so a save built from an older copy never silently replaces a newer one,
        unless it is the stored Scene exactly (an idempotent retry).
        """
        stored = self._state.scenes.get(scene.scene_id)
        if stored is not None and scene.revision <= stored.revision and scene != stored:
            raise RuntimeConflict("scene_revision_conflict")
        self._state.scenes[scene.scene_id] = scene

    @staticmethod
    def _scene_ids(scene: Scene) -> set[str]:
        ids = {scene.scene_id}
        for child in scene.children:
            ids.update(Runtime._scene_ids(child.scene))
        return ids

    def delete_scene(self, scene_id: str, expected_revision: int) -> None:
        """Delete an unused authored Scene with optimistic revision checking."""
        stored = self._state.scenes.get(scene_id)
        if stored is None:
            raise RuntimeConflict("scene_missing")
        if stored.revision != expected_revision:
            raise RuntimeConflict("scene_revision_conflict")

        program_ids = [p.program_id for p in self._state.programs.values() if p.scene_id == scene_id]
        run_ids = [r.run_id for r in self._state.runs.values()
                   if r.active and scene_id in self._scene_ids(r.scene)]
        queued_activation_ids = [q.activation_id for q in self._state.queue
                                 if scene_id in self._scene_ids(q.scene)]
        scene_ids = [parent_id for parent_id, scene in self._state.scenes.items()
                     if parent_id != scene_id and scene_id in self._scene_ids(scene)]
        default_scene = (self._state.default_scene is not None
                         and self._state.default_scene.scene_id == scene_id)
        if program_ids or run_ids or queued_activation_ids or scene_ids or default_scene:
            raise SceneInUse(program_ids=program_ids, run_ids=run_ids,
                             queued_activation_ids=queued_activation_ids, scene_ids=scene_ids,
                             default_scene=default_scene)
        del self._state.scenes[scene_id]

    def frame_references(self, frame_id: str, now: float | None = None) -> dict[str, tuple[str, ...]]:
        """Return stored, queued, and live references to a Frame id.

        Run and queue Scenes are snapshots, so inspect them independently of the
        current definitions. When `now` is supplied, evaluate live state through
        a detached projection so expired queue entries and logically ended Runs
        do not block a delete merely because the next scheduler tick has not run.
        Terminal Run history is deliberately excluded; the owned Runtime is not
        advanced by this read.
        """
        if now is not None:
            projected = self._copy()
            projected.advance(now)
            return projected._frame_references(frame_id, now)
        return self._frame_references(frame_id, now)

    def _frame_references(self, frame_id: str, now: float | None) -> dict[str, tuple[str, ...]]:
        target = f"frame:{frame_id}"
        scene_ids = tuple(sorted(
            scene_id for scene_id, scene in self._state.scenes.items()
            if target in scene.participants
        ))
        queued = tuple(sorted(
            item.activation_id for item in self._state.queue
            if target in item.scene.participants
        ))
        runs = tuple(sorted(
            run.run_id for run in self._state.runs.values()
            if run.active and target in run.scene.participants
        ))
        program_ids = tuple(sorted(
            program.program_id for program in self._state.programs.values()
            if program.scene_id in scene_ids
        ))
        return {
            "scene_ids": scene_ids,
            "program_ids": program_ids,
            "queued_activation_ids": queued,
            "run_ids": runs,
        }

    @staticmethod
    def _scene_source_refs(scene: Scene) -> set[str]:
        refs = {ref for contribution in (*scene.contributions, *scene.outro_contributions)
                for ref in contribution.source_refs}
        for child in scene.children:
            refs.update(Runtime._scene_source_refs(child.scene))
        return refs

    def scenes_using_sources(self, refs: set[str]) -> tuple[str, ...]:
        return tuple(sorted(scene_id for scene_id, scene in self._state.scenes.items()
                            if self._scene_source_refs(scene) & refs))

    def planning_source_refs(self) -> set[str]:
        """Refs that can contribute to a current or future Run projection."""
        refs = set()
        for scene in self._state.scenes.values():
            refs.update(self._scene_source_refs(scene))
        for run in self._state.runs.values():
            if run.active:
                refs.update(self._scene_source_refs(run.scene))
        for queued in self._state.queue:
            refs.update(self._scene_source_refs(queued.scene))
        return refs

    def planning_selections(self) -> dict[str, SceneSelection]:
        """Roadmap 5a (slice O1): the selection of every Scene that can contribute to a current
        or future Run projection (configured Scenes, active Runs' and queued Scenes, each with
        its children), by scene id; a Run's admitted snapshot wins over the configured Scene of
        the same id. Coordination reads the shown history for these Scenes."""
        selections: dict[str, SceneSelection] = {}

        def walk(scene: Scene) -> None:
            selections[scene.scene_id] = scene.selection
            for child in scene.children:
                walk(child.scene)

        for scene in self._state.scenes.values():
            walk(scene)
        for queued in self._state.queue:
            walk(queued.scene)
        for run in self._state.runs.values():
            if run.active:
                walk(run.scene)
        return selections

    def revise_source_refs(self, old_refs: set[str], new_ref: str) -> tuple[str, ...]:
        """Revise future Scene definitions; admitted and queued snapshots stay intact."""
        def revise(scene: Scene) -> Scene:
            def contributions(items):
                return tuple(item.model_copy(update={
                    "source_refs": tuple(new_ref if ref in old_refs else ref for ref in item.source_refs)
                }) if any(ref in old_refs for ref in item.source_refs) else item for item in items)

            body = contributions(scene.contributions)
            outro = contributions(scene.outro_contributions)
            children = tuple(child.model_copy(update={"scene": revise(child.scene)})
                             for child in scene.children)
            if (body, outro, children) == (scene.contributions, scene.outro_contributions, scene.children):
                return scene
            return scene.model_copy(update={"revision": scene.revision + 1,
                                            "contributions": body, "outro_contributions": outro,
                                            "children": children})

        changed = []
        for scene_id, scene in self._state.scenes.items():
            updated = revise(scene)
            if updated is not scene:
                self._state.scenes[scene_id] = updated
                changed.append(scene_id)
        return tuple(sorted(changed))

    def _check_program(self, draft: ProgramDraft) -> None:
        """ProgramRefused for a draft that cannot be stored: an unknown Scene, then the first
        When problem (central.house_time.calculator.when_problems, their one home)."""
        if draft.scene_id not in self._state.scenes:
            raise ProgramRefused("unknown-scene")
        problems = calculator.when_problems(draft.when, self._house)
        if problems:
            raise ProgramRefused(problems[0])

    def program_whens(self) -> tuple[When, ...]:
        """Every stored Program's When (Settings › House checks them before the location goes)."""
        return tuple(program.when for program in self._state.programs.values())

    def create_program(self, program: Program, now: float) -> Program:
        """Store a new Program at revision 1. Its windows are admitted from `now` on: a window
        already started is not (a new Schedule starts with its next window). The same Program
        again is an idempotent retry; another under a taken id is refused "changed"."""
        if program.revision != 1:
            raise ValueError("a new Program has revision 1")
        self.advance(now)
        stored = self._state.programs.get(program.program_id)
        if stored is not None:
            if stored == program:
                return stored
            raise ProgramRefused("changed", stored)
        self._check_program(program)
        self._state.programs[program.program_id] = program
        self._state.program_from[program.program_id] = self._state.now
        self.advance(now)
        return program

    def save_program(self, expected: Program, draft: ProgramDraft, now: float) -> Program:
        """Replace `expected` with `draft` at the next revision. The window playing now keeps its
        end (R10): the new rule's windows are admitted from the later of now and that end."""
        saved, playing = self._replace_program(expected, draft, now)
        if playing is not None:
            ends = [run.program_ends_at for run in playing if run.program_ends_at is not None]
            self._state.program_from[saved.program_id] = max([self._state.now, *ends])
            self.advance(now)
        return saved

    def save_program_and_apply_now(self, expected: Program, draft: ProgramDraft, now: float) -> Program:
        """As `save_program`, but applied at once: the Program's playing Runs finish gracefully at
        `now`, and the new rule's window covering `now`, if any, is admitted at once (from `now`
        to its end, under its own identity), the finishing Runs left out of the protection check
        (as `activate`'s restart leaves out the Runs it replaces). Later windows follow the new
        rule from `now`."""
        saved, playing = self._replace_program(expected, draft, now)
        if playing is None:
            return saved
        for run in playing:
            run.scheduled_finish_at = min(run.scheduled_finish_at or now, now)
        window = calculator.window_at(saved.when, self._house, self._state.now)
        if window is not None:
            identity = saved.window_id(window.start)
            scene = self._state.scenes[saved.scene_id]
            rank = PRIORITY_RANK[saved.priority]
            denial = self._protected_conflict(scene, rank, False, {run.run_id for run in playing})
            self._state.admissions[identity] = (
                _refusal(identity, denial) if denial else self._admit(
                    scene, identity, self._state.now, rank,
                    program_id=saved.program_id, program_end=window.end))
        self._state.program_from[saved.program_id] = self._state.now
        self.advance(now)
        return saved

    def _replace_program(self, expected: Program, draft: ProgramDraft,
                         now: float) -> tuple[Program, list[_Run] | None]:
        """Store `draft` as `expected`'s next revision after advancing to `now`, and answer it with
        the Program's playing root Runs; None for them when the same save was already stored (an
        idempotent retry, nothing changed). ProgramRefused "missing", "changed" or a draft refusal."""
        self.advance(now)
        stored = self._state.programs.get(expected.program_id)
        if stored is None:
            raise ProgramRefused("missing")
        saved = Program(**draft.model_dump(), program_id=expected.program_id,
                        revision=expected.revision + 1)
        if stored == saved:
            return stored, None
        if stored != expected:
            raise ProgramRefused("changed", stored)
        self._check_program(draft)
        playing = [run for run in self._state.runs.values()
                   if run.parent_id is None and run.active and run.program_id == saved.program_id]
        self._state.programs[saved.program_id] = saved
        return saved, playing

    def remove_program(self, program_id: str, revision: int, now: float) -> RuntimeView:
        """Forget a Program; its active Runs finish gracefully at `now`. It is forgotten before
        Runtime advances to `now`, so no window of it starts then."""
        now = _finite(now)
        if self._state.now is not None and now < self._state.now:
            raise ValueError("Runtime cannot move backwards in UTC")
        stored = self._state.programs.get(program_id)
        if stored is None:
            raise ProgramRefused("missing")
        if stored.revision != revision:
            raise ProgramRefused("changed", stored)
        del self._state.programs[program_id]
        self._state.program_from.pop(program_id, None)
        for run in self._state.runs.values():
            if run.program_id == program_id and run.active:
                run.scheduled_finish_at = min(run.scheduled_finish_at or now, now)
        return self.advance(now)

    def set_default_scene(self, scene_id: str | None, now: float) -> RuntimeView:
        """Choose the Scene that plays wherever no Schedule does (E5), or none. The previous
        choice's Run finishes gracefully at `now` and the new one is admitted then
        (`_process`)."""
        self.advance(now)
        current = self._state.default_scene
        if scene_id is not None and scene_id not in self._state.scenes:
            raise ProgramRefused("unknown-scene")
        if (current.scene_id if current else None) == scene_id:
            return self.advance(now)
        self._state.default_scene = None if scene_id is None else DefaultScene(
            scene_id=scene_id, revision=current.revision + 1 if current else 1)
        return self.advance(now)

    def export_state(self) -> dict:
        """The persisted form; the only path by which runtime state is stored.

        Rollback compatibility: builds before `Admission.blocking_run_id` forbid
        the key, so it is written only when set. A state holding no protection
        refusal therefore still restores on the previous Central build.
        """
        state = self._state.model_dump(mode="json")
        for admission in state["admissions"].values():
            if admission["blocking_run_id"] is None:
                del admission["blocking_run_id"]
        return state

    @classmethod
    def restore(cls, state: dict, *, house: HouseTime) -> Runtime:
        runtime = cls(house=house)
        runtime._state = _State.model_validate(state)
        # Early MVP v1 exports used only global descendant order. Adopt that
        # established ordering once, then allocate independently within a root.
        root_orders: dict[str, int] = {}
        for run in runtime._state.runs.values():
            if "local_order" not in run.model_fields_set:
                run.local_order = run.order if run.parent_id else 0
            root_orders[run.root_id] = max(root_orders.get(run.root_id, 0), run.local_order)
        for root in runtime._state.runs.values():
            if root.parent_id is None:
                root.descendant_sequence = max(root.descendant_sequence, root_orders[root.run_id])
        return runtime

    def _copy(self) -> Runtime:
        """A detached copy, so a projection never touches the owned state."""
        return self.restore(self.export_state(), house=self._house)

    def project(self, now: float, *, max_events: int = 10000) -> RuntimeView:
        return self._copy().advance(now, max_events=max_events)

    def operator_projection(self, now: float, *, max_events: int = 10000) -> OperatorProjection:
        """One restore and advance, as `project`, read for the operator console."""
        projected = self._copy()
        view = projected.advance(now, max_events=max_events)
        state = projected._state
        since = view.now - OPERATOR_HISTORY_SECONDS
        runs = tuple(run for run in view.runs if run.ended_at is None or run.ended_at >= since)
        return OperatorProjection(
            current=view.model_copy(update={"runs": runs}),
            protected_frames={
                run.run_id: protected for run in runs
                if (protected := state.runs[run.run_id].scene.protected_frames)
            },
            program_windows={
                program.program_id: projected._program_windows(program, view.now)
                for program in state.programs.values()
            },
            default_scene=state.default_scene,
        )

    def _program_windows(self, program: Program, now: float) -> ProgramWindows:
        """The card's windows by the rule Runtime admits by (R10): `current` is the window the
        Program is playing (under whichever revision admitted it), else its current rule's window
        covering `now` once that rule applies (a refused one shows its refusal); `next` is the
        current rule's first window starting after `now` and at or after its `program_from`."""
        since = self._program_from(program)
        playing = self._playing_window(program)
        try:
            covering = calculator.window_at(program.when, self._house, now)
            following = next((window for window in _windows_after(program.when, self._house, now)
                              if window.start > now and window.start >= since), None)
        except ValueError:          # a When the house can no longer place
            covering = following = None
        if playing is None and covering is not None and covering.start >= since:
            playing = (covering, program.window_id(covering.start))

        def view(window: Window | None, identity: str | None) -> ProgramWindowView | None:
            if window is None or identity is None:
                return None
            return ProgramWindowView(
                start_at=window.start, end_at=window.end,
                start=calculator.local_date_time(self._house, window.start),
                end=calculator.local_date_time(self._house, window.end),
                admission=self._state.admissions.get(identity))

        return ProgramWindows(
            current=view(*playing) if playing else None,
            next=view(following, None if following is None else program.window_id(following.start)))

    def program_windows_between(self, program: Program, start: float, end: float) -> tuple[tuple[Window, bool], ...]:
        """`program`'s windows over [start, end), each with whether it plays by the rule Runtime
        admits by (R10): its current rule's windows (those starting before its `program_from`, as
        one a save or a creation made mid-window, do not play), and the window a Run of an earlier
        revision is still playing (a save keeps that window's end). The week page's one source."""
        since = self._program_from(program)
        try:
            found = [(window, window.start >= since)
                     for window in calculator.windows(program.when, self._house, start, end)]
        except ValueError:          # a When the house can no longer place
            found = []
        playing = self._playing_window(program)
        if playing is not None and playing[0].start < end and playing[0].end > start:
            found = [item for item in found if item[0] != playing[0]] + [(playing[0], True)]
        return tuple(sorted(found, key=lambda item: item[0].start))

    def _program_from(self, program: Program) -> float:
        """From when `program`'s current rule is admitted (unbounded for a state stored before it)."""
        return self._state.program_from.get(program.program_id, float("-inf"))

    def _playing_window(self, program: Program) -> tuple[Window, str] | None:
        """The window an active root Run of `program` plays (the current revision's first; its
        start from its identity, `Program.window_id`) and its identity, or None."""
        runs = sorted((run for run in self._state.runs.values()
                       if run.parent_id is None and run.active and run.program_id == program.program_id
                       and run.program_ends_at is not None and run.activation_id is not None),
                      key=lambda run: (not run.activation_id.startswith(program.window_prefix), -run.started_at))
        if not runs:
            return None
        run = runs[0]
        # The window's start is its identity's (Save and apply now starts its Run later, at `now`).
        start = float(run.activation_id.rsplit(":", 1)[1])
        return Window(start=start, end=run.program_ends_at), run.activation_id

    def timeline(
        self, start: float, end: float, *, max_events: int = 10000
    ) -> tuple[RuntimeView, ...]:
        """Pure settled views at start and every discrete boundary in [start, end)."""
        start, end = _finite(start), _finite(end)
        if end <= start:
            raise ValueError("timeline end must follow start")
        budget = _TransitionBudget(max_events)
        projected = self._copy()
        views = [projected._advance(start, budget)]
        budget.consume()
        while (boundary := projected._next_event()) is not None and boundary < end:
            views.append(projected._advance(boundary, budget))
            budget.consume()
        return tuple(views)

    def activate(
        self,
        scene_id: str,
        activation_id: str,
        now: float,
        *,
        priority: int = 0,
        repeat: Literal["ignore", "restart", "queue"] = "ignore",
        force: bool = False,
        expires_at: float | None = None,
    ) -> Admission:
        if repeat not in ("ignore", "restart", "queue"):
            raise ValueError("unknown repeat policy")
        if not activation_id or not isinstance(activation_id, str):
            raise ValueError("activation_id must be a nonempty string")
        if activation_id.startswith((PROGRAM_PREFIX, DEFAULT_PREFIX)):
            raise ValueError("program and Default Scene identities are reserved for scheduled ingress")
        if priority <= DEFAULT_SCENE_RANK:
            raise ValueError("an operator request ranks above the Default Scene (priority 0 and up)")
        self.advance(now)
        if activation_id in self._state.admissions:
            return self._state.admissions[activation_id]
        if scene_id not in self._state.scenes:
            raise ValueError(f"unknown Scene: {scene_id}")
        scene = self._state.scenes[scene_id]
        matches = self._matching(scene_id)
        if matches and repeat == "ignore":
            result = Admission(
                activation_id=activation_id, status="ignored", run_id=matches[-1].run_id
            )
        elif matches and repeat == "queue":
            if expires_at is None:
                raise ValueError("queued activation requires expires_at")
            expires_at = _finite(expires_at)
            if expires_at <= now:
                result = Admission(activation_id=activation_id, status="expired")
            elif len(self._state.queue) >= 16:
                result = Admission(
                    activation_id=activation_id, status="rejected", reason="queue_full"
                )
            else:
                self._state.queue.append(
                    _Queued(
                        activation_id=activation_id, scene=scene, priority=priority,
                        force=force, expires_at=expires_at,
                    )
                )
                result = Admission(activation_id=activation_id, status="queued")
        else:
            excluded = {r.run_id for r in matches} if repeat == "restart" else set()
            denial = self._protected_conflict(scene, priority, force, excluded)
            if denial:
                result = _refusal(activation_id, denial)
            else:
                for run in matches if repeat == "restart" else ():
                    self._cancel(run, now)
                result = self._admit(scene, activation_id, now, priority)
        self._state.admissions[activation_id] = result
        self.advance(now)
        return result

    def finish(self, run_id: str, now: float) -> RuntimeView:
        now = _finite(now)
        if self._state.now is not None and now < self._state.now:
            raise ValueError("Runtime cannot move backwards in UTC")
        run = self._state.runs.get(run_id)
        if run and run.active:
            run.scheduled_finish_at = min(run.scheduled_finish_at or now, now)
        return self.advance(now)

    def cancel(self, run_id: str, now: float) -> RuntimeView:
        self.advance(now)
        run = self._state.runs.get(run_id)
        if run:
            self._cancel(run, now)
        return self.advance(now)

    def advance(self, now: float, *, max_events: int = 10000) -> RuntimeView:
        return self._advance(now, _TransitionBudget(max_events))

    def _advance(self, now: float, budget: _TransitionBudget, *, holds: bool = True) -> RuntimeView:
        now = _finite(now)
        if self._state.now is not None and now < self._state.now:
            raise ValueError("Runtime cannot move backwards in UTC")
        # A Program window wholly missed by this advance (ended at or before `now`) is skipped;
        # on a cold start that leaves only the window covering `now`.
        self._target = now
        try:
            while True:
                self._skip_leaf_cycles(now)
                event = self._next_event()
                if event is None or event > now:
                    break
                budget.consume()
                self._process(event, budget)
        finally:
            self._target = None
        self._state.now = now
        self._forget(now)
        return self._view(now, budget=budget if holds else None)

    def _forget(self, now: float) -> None:
        """The one retention rule (Runtime's state stays bounded however long it runs): a root
        Run that ended, and whose Program window (if any) ended, more than
        OPERATOR_HISTORY_SECONDS before `now` is forgotten with its admission; so is a refused
        Program window's admission once no window of its Program's current revision ending
        after that instant has its identity. A forgotten identity is never consulted again:
        only windows ending after the floor are admitted. An operator retry of a forgotten
        activation id is a new request."""
        cutoff = now - OPERATOR_HISTORY_SECONDS
        for run in list(self._state.runs.values()):
            if (run.parent_id is None and not run.active and run.ended_at is not None
                    and run.ended_at < cutoff
                    and (run.program_ends_at is None or run.program_ends_at < cutoff)):
                del self._state.runs[run.run_id]
                if run.activation_id is not None:
                    self._state.admissions.pop(run.activation_id, None)
        recent = {program.window_id(window.start)
                  for program in self._state.programs.values()
                  for window in _windows_after(program.when, self._house, cutoff)}
        for identity, admission in list(self._state.admissions.items()):
            if (identity.startswith(PROGRAM_PREFIX) and admission.run_id is None
                    and identity not in recent):
                del self._state.admissions[identity]

    def _floor(self) -> float | None:
        """The instant before which no Program window or Default Scene is admitted: the advance's
        target while advancing, else the processed instant."""
        candidates = [t for t in (self._state.now, self._target) if t is not None]
        return max(candidates, default=None)

    def _pending_windows(self) -> list[tuple[float, str, Program, Window]]:
        """Every Program window still to be admitted, (start, program_id, Program, Window), in
        start order: it ends after the floor, starts at or after its Program's `program_from`
        and at or after the processed instant (a window is never admitted backdated, e.g. one a
        house zone or place change moved into the past: the change applies from the next
        window, as a save does, R10), and its identity has no admission yet."""
        floor = self._floor()
        if floor is None:
            return []
        processed = self._state.now
        pending = []
        for program in self._state.programs.values():
            since = self._state.program_from.get(program.program_id, floor)
            if processed is not None:
                since = max(since, processed)
            for window in _windows_after(program.when, self._house, floor):
                if window.start >= since and program.window_id(window.start) not in self._state.admissions:
                    pending.append((window.start, program.program_id, program, window))
        pending.sort(key=lambda item: item[:2])
        return pending

    def _default_runs(self) -> list[_Run]:
        return [run for run in self._state.runs.values()
                if run.parent_id is None and run.active and run.activation_id is not None
                and run.activation_id.startswith(DEFAULT_PREFIX)]

    def _default_prefix(self) -> str | None:
        """What the identity of a Run of the Default Scene as chosen and stored now starts with:
        the choice's revision and its Scene's revision, so a new choice or an edit of its Scene
        is the next Run (requirements: "Authored configuration edits default to the next
        Run"). None when none is chosen or its Scene is gone."""
        chosen = self._state.default_scene
        if chosen is None or chosen.scene_id not in self._state.scenes:
            return None
        scene = self._state.scenes[chosen.scene_id]
        return f"{DEFAULT_PREFIX}{chosen.scene_id}:{chosen.revision}:{scene.revision}:"

    def _stale_defaults(self) -> list[_Run]:
        """The active Default Scene Runs not under the current prefix, not yet asked to finish."""
        prefix = self._default_prefix()
        return [run for run in self._default_runs()
                if (prefix is None or not run.activation_id.startswith(prefix))
                and run.scheduled_finish_at is None and run.finish_requested_at is None]

    def _default_due(self) -> Scene | None:
        """The Default Scene's Scene, repeating until replaced (it never ends by itself: one
        perpetual Run), when it is set, none of its Runs under the current prefix is active, and
        no protection refuses it (a refusal is never recorded: it is retried at the next
        event)."""
        prefix = self._default_prefix()
        if prefix is None:
            return None
        if any(run.activation_id.startswith(prefix) for run in self._default_runs()):
            return None
        chosen = self._state.scenes[self._state.default_scene.scene_id]
        # It never protects either (frame design rule 3: it is beneath every Schedule): a
        # protecting copy would refuse every Schedule on its Frames for as long as it plays.
        scene = _unprotected(chosen).model_copy(update={"loop": True, "duration_seconds": None})
        if self._protected_conflict(scene, DEFAULT_SCENE_RANK, False):
            return None
        return scene

    def _matching(self, scene_id: str) -> list[_Run]:
        return [
            run for run in self._state.runs.values()
            if run.parent_id is None and run.active and run.scene.scene_id == scene_id
        ]

    def _protected_conflict(
        self, scene: Scene, priority: int, force: bool, excluded: set[str] | None = None
    ) -> tuple[str, str] | None:
        """The refusal reason and the root Run that refuses `scene`, if any."""
        frames = {target for target in scene.participants if target.startswith("frame:")}
        for run in self._state.runs.values():
            if run.parent_id is not None or not run.active or run.run_id in (excluded or set()):
                continue
            if not frames.intersection(run.scene.participants):
                continue
            if frames.intersection(run.scene.protected_frames) and not force:
                return "protected_frames", run.run_id
            if scene.protected_frames.intersection(run.scene.participants) and run.priority > priority:
                return "protection_not_visible", run.run_id
        return None

    def _admit(
        self, scene: Scene, identity: str, start: float, priority: int,
        *, parent: _Run | None = None, program_end: float | None = None,
        program_id: str | None = None,
    ) -> Admission:
        self._state.sequence += 1
        run_id = _run_id(("child:" if parent else "activation:") + identity)
        local_order = 0
        if parent:
            root = self._state.runs[parent.root_id]
            root.descendant_sequence += 1
            local_order = root.descendant_sequence
        run = _Run(
            run_id=run_id, root_id=parent.root_id if parent else run_id,
            parent_id=parent.run_id if parent else None,
            scene=scene, started_at=start, priority=priority, order=self._state.sequence,
            root_order=parent.root_order if parent else self._state.sequence,
            local_order=local_order,
            program_id=program_id, program_ends_at=program_end,
            activation_id=None if parent else identity,
        )
        self._state.runs[run_id] = run
        if parent:
            parent.children.append(run_id)
        return Admission(activation_id=identity, status="admitted", run_id=run_id)

    def _request_finish(self, run: _Run, at: float) -> None:
        if run.phase != "body" or run.finish_requested_at is not None:
            return
        run.finish_requested_at = at
        for child_id in list(run.children):
            self._request_finish(self._state.runs[child_id], at)

    def _end(self, run: _Run, at: float, phase: Literal["completed", "cancelled"]) -> None:
        run.phase, run.ended_at = phase, at
        if run.parent_id:
            parent = self._state.runs[run.parent_id]
            parent.children.remove(run.run_id)
            parent.children_finished_at = max(parent.children_finished_at or at, at)
            del self._state.runs[run.run_id]

    def _cancel(self, run: _Run, at: float) -> None:
        if not run.active:
            return
        for child_id in list(run.children):
            self._cancel(self._state.runs[child_id], at)
        self._end(run, at, "cancelled")

    def _skip_leaf_cycles(self, until: float) -> None:
        """A month-long background needs no per-cycle lifecycle history."""
        boundaries = [start for start, *_ in self._pending_windows()]
        for run in self._state.runs.values():
            if (
                run.parent_id is not None or run.phase != "body" or not run.scene.loop
                or run.scene.children or run.body_done_at is not None
            ):
                continue
            future_boundaries = [t for t in boundaries if t >= run.cycle_start]
            limit = min([until, *future_boundaries])
            stop = run.stop_at
            ending = stop is not None and stop <= limit
            if ending:
                limit = stop
            cycles = (limit - run.started_at) / run.scene.cycle_seconds
            if not math.isfinite(cycles):
                raise RuntimeBudgetExceeded("cycle index exceeds supported arithmetic range")
            index = math.ceil(cycles) - 1 if ending else math.floor(cycles)
            run.cycle_index = max(run.cycle_index, index, 0)

    def _next_event(self) -> float | None:
        events = [start for start, *_ in self._pending_windows()[:1]]
        if self._default_due() is not None or self._stale_defaults():
            events.append(self._floor())
        events.extend(item.expires_at for item in self._state.queue)
        for run in self._state.runs.values():
            if run.phase == "outro":
                events.append(run.outro_started_at + run.scene.outro_seconds)
            elif run.phase == "body":
                if run.finish_requested_at is None and run.stop_at is not None:
                    events.append(run.stop_at)
                if run.body_done_at is None:
                    events.append(run.cycle_end)
                    if run.finish_requested_at is None:
                        events.extend(
                            run.cycle_start + child.delay_seconds
                            for index, child in enumerate(run.scene.children)
                            if index not in run.launched_children
                        )
                elif not run.children:
                    events.append(max(run.body_done_at, run.children_finished_at or run.body_done_at))
        # A newly freed matching/protected predecessor can release queued work immediately.
        for item in self._state.queue:
            if (
                not self._matching(item.scene.scene_id)
                and not self._protected_conflict(item.scene, item.priority, item.force)
            ):
                releases = [r.ended_at for r in self._state.runs.values() if r.ended_at is not None]
                events.append(max([self._state.now or 0, *releases]))
        return min(events, default=None)

    def _process(self, at: float, budget: _TransitionBudget) -> None:
        # Natural stops precede child/cycle starts at the same instant.
        for run in list(self._state.runs.values()):
            if run.phase != "body":
                continue
            if run.stop_at is not None and run.stop_at <= at:
                self._request_finish(run, run.stop_at)
            if not run.scene.loop and run.cycle_end <= at:
                self._request_finish(run, run.cycle_end)
        # Release existing ownership before admitting a successor at the exact
        # boundary. Descendants settle before parents; positive outros may keep
        # the reservation. This fixed point emits no physical effects.
        changed = True
        while changed:
            changed = False
            for run in reversed(list(self._state.runs.values())):
                if run.run_id not in self._state.runs or not run.active:
                    continue
                budget.consume()
                if run.phase == "outro":
                    end = run.outro_started_at + run.scene.outro_seconds
                    if end <= at:
                        self._end(run, end, "completed")
                        changed = True
                    continue
                if run.body_done_at is None and run.cycle_end <= at:
                    if run.finish_requested_at is not None:
                        run.body_done_at = run.cycle_end
                    else:
                        run.cycle_index += 1
                        run.launched_children.clear()
                    changed = True
                if run.body_done_at is not None and not run.children:
                    start = max(run.body_done_at, run.children_finished_at or run.body_done_at)
                    if run.scene.outro_seconds:
                        run.outro_started_at, run.phase = start, "outro"
                    else:
                        self._end(run, start, "completed")
                    changed = True
        for start, _, program, window in self._pending_windows():
            if start > at:
                break
            budget.consume()
            identity = program.window_id(window.start)
            scene = self._state.scenes[program.scene_id]
            rank = PRIORITY_RANK[program.priority]
            denial = self._protected_conflict(scene, rank, False)
            result = (
                _refusal(identity, denial)
                if denial else self._admit(
                    scene, identity, window.start, rank,
                    program_id=program.program_id, program_end=window.end,
                )
            )
            self._state.admissions[identity] = result
        # A Default Scene Run of an earlier choice or Scene revision finishes gracefully; the
        # current one is admitted once no protection refuses it.
        for run in self._stale_defaults():
            budget.consume()
            run.scheduled_finish_at = at
        if (default := self._default_due()) is not None:
            budget.consume()
            identity = f"{self._default_prefix()}{at:.17g}"
            self._state.admissions[identity] = self._admit(default, identity, at, DEFAULT_SCENE_RANK)
        for run in list(self._state.runs.values()):
            if run.phase != "body" or run.finish_requested_at is not None:
                continue
            for index, child in enumerate(run.scene.children):
                start = run.cycle_start + child.delay_seconds
                if index not in run.launched_children and start <= at:
                    budget.consume()
                    run.launched_children.add(index)
                    self._admit(
                        child.scene, f"{run.run_id}/{run.cycle_index}/{index}",
                        start, run.priority, parent=run,
                    )
        for item in list(self._state.queue):
            if item.expires_at <= at:
                budget.consume()
                self._state.admissions[item.activation_id] = Admission(
                    activation_id=item.activation_id, status="expired"
                )
            elif self._matching(item.scene.scene_id) or self._protected_conflict(
                item.scene, item.priority, item.force
            ):
                continue
            else:
                budget.consume()
                self._state.admissions[item.activation_id] = self._admit(
                    item.scene, item.activation_id, at, item.priority
                )
            self._state.queue.remove(item)

    def _playing_at(self, at: float, budget: _TransitionBudget) -> tuple[Intent, ...]:
        """Every contribution playing at `at`, see-through ones included (the Player draws a
        kept photo only as its fallback, never beneath a playing layer), from a detached
        projection without the after-end rules (so it never recurses), spending the caller's
        transition budget."""
        view = self._copy()._advance(at, budget, holds=False)
        return tuple(intent for intent in view.contributions
                     if intent.interval_start <= at < intent.interval_end)

    def _view(self, now: float, *, budget: _TransitionBudget | None) -> RuntimeView:
        """The view at `now`. With a budget, spent on what plays at a layer's end
        (`_playing_at`): a kept photo's final cycle is held when nothing else plays on its
        Frame (`_may_hold_to_the_end`); and the owner's "When the top show ends, it gets out of
        the way and the show underneath takes over": an ending keeps nothing only when no
        other Run plays on its Frame at its end, and a photo is kept only when no Run beneath
        it plays on its Frame at its cycle's end."""
        intents: list[Intent] = []
        runs = sorted(self._state.runs.values(), key=lambda r: r.order)
        playing: dict[float, tuple[Intent, ...]] = {}

        def others(run: _Run, at: float, *, beneath: bool = False) -> frozenset[str]:
            if at not in playing:
                playing[at] = self._playing_at(at, budget)
            mine = (run.priority, run.root_order, run.local_order)
            return frozenset(intent.target for intent in playing[at]
                             if intent.run_id != run.run_id
                             and (not beneath or intent.precedence < mine))

        for run in runs:
            if not run.active or (run.phase == "body" and run.body_done_at is not None):
                continue
            start = run.outro_started_at if run.phase == "outro" else run.cycle_start
            end = start + (run.scene.outro_seconds if run.phase == "outro" else run.scene.cycle_seconds)
            definitions = run.scene.outro_contributions if run.phase == "outro" else run.scene.contributions
            position = max(0.0, now - start)
            for contribution in definitions:
                fade_out = contribution.fade_out_seconds
                after_end = contribution.after_end
                if budget is not None:
                    if (fade_out and _may_hold_to_the_end(run, contribution)
                            and contribution.target not in others(run, end)):
                        fade_out = 0
                    if (after_end == "keep_nothing" and run.phase == "outro"
                            and contribution.target in others(run, end)):
                        after_end = "leave_as_is"
                    if (after_end == "keep_this_photo" and run.phase == "body"
                            and contribution.target in others(run, end, beneath=True)):
                        after_end = "leave_as_is"
                opacity = contribution.opacity
                if contribution.fade_in_seconds:
                    opacity *= min(1.0, position / contribution.fade_in_seconds)
                if fade_out:
                    opacity *= min(1.0, max(0.0, end - now) / fade_out)
                fraction = min(1.0, position / (end - start))
                value = (
                    contribution.ramp_from
                    + (contribution.ramp_to - contribution.ramp_from) * fraction
                    if contribution.kind == "actuator" else None
                )
                intents.append(Intent(
                    target=contribution.target, kind=contribution.kind,
                    run_id=run.run_id, root_id=run.root_id, scene_id=run.scene.scene_id,
                    scene_revision=run.scene.revision, role=contribution.role,
                    source_refs=contribution.source_refs, asset_refs=contribution.asset_refs,
                    priority=run.priority, root_order=run.root_order, admission_order=run.local_order,
                    logical_origin=run.started_at, logical_position=max(0.0, now - run.started_at),
                    cycle_index=run.cycle_index, cycle_position=position,
                    interval_start=start, interval_end=end, opacity=opacity,
                    base_opacity=contribution.opacity,
                    fade_in_seconds=contribution.fade_in_seconds,
                    fade_out_seconds=fade_out,
                    after_end=after_end,
                    actuator_value=value, phase=run.phase,
                    selection=run.scene.selection,
                ))
        winners: dict[str, Intent] = {}
        for intent in intents:
            if intent.target not in winners or intent.precedence > winners[intent.target].precedence:
                winners[intent.target] = intent
        return RuntimeView(
            now=now,
            runs=tuple(RunView(
                run_id=r.run_id, root_id=r.root_id, parent_id=r.parent_id,
                scene_id=r.scene.scene_id, scene_revision=r.scene.revision,
                started_at=r.started_at, phase=r.phase, participants=r.scene.participants,
                children=tuple(r.children), finish_requested_at=r.finish_requested_at,
                ended_at=r.ended_at, program_id=r.program_id, priority=r.priority,
                activation_id=r.activation_id,
            ) for r in runs),
            contributions=tuple(intents), visible=tuple(winners[k] for k in sorted(winners)),
        )


class ActuatorRecord(FrozenModel):
    at: FiniteFloat
    target: Target
    run_id: str
    value: Unit


class RecordingActuator:
    """Current-state test adapter; authorization remains coordination's decision."""

    def __init__(self) -> None:
        self.records: list[ActuatorRecord] = []
        self.current: dict[str, ActuatorRecord] = {}

    def apply(
        self, view: RuntimeView, *, now: float, authorized_run_ids: set[str]
    ) -> tuple[ActuatorRecord, ...]:
        if view.now != _finite(now):
            raise ValueError("only the current instant may command actuators")
        emitted = []
        for intent in view.visible:
            if intent.kind != "actuator" or intent.run_id not in authorized_run_ids:
                continue
            record = ActuatorRecord(
                at=now, target=intent.target, run_id=intent.run_id, value=intent.actuator_value
            )
            previous = self.current.get(intent.target)
            if previous is None or (previous.run_id, previous.value) != (record.run_id, record.value):
                self.current[intent.target] = record
                self.records.append(record)
                emitted.append(record)
        return tuple(emitted)
