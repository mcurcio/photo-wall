"""Deterministic Scene lifecycle and intent, without I/O or media selection.

See docs/module-runtime.md. UTC is always supplied by the caller. Persist an
export in the same transaction as activation ingress before publishing its intent.
"""

from __future__ import annotations

import hashlib
import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

Target = Annotated[str, Field(pattern=r"^(frame|actuator):[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")]
Seconds = Annotated[FiniteFloat, Field(ge=0)]
PositiveSeconds = Annotated[FiniteFloat, Field(gt=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Unit = Annotated[FiniteFloat, Field(ge=0, le=1)]


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


class Program(FrozenModel):
    program_id: Identifier
    scene_id: Identifier
    starts_at: FiniteFloat
    ends_at: FiniteFloat
    priority: int = 0

    @model_validator(mode="after")
    def window(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("Program end must follow start")
        return self

    @property
    def activation_id(self) -> str:
        return f"program:{self.program_id}:{self.starts_at:.17g}"


class Admission(FrozenModel):
    activation_id: str
    status: Literal["admitted", "ignored", "queued", "rejected", "expired"]
    run_id: str | None = None
    reason: str | None = None


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
    actuator_value: Unit | None = None
    phase: Literal["body", "outro"]

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


class RuntimeView(FrozenModel):
    now: FiniteFloat
    runs: tuple[RunView, ...]
    contributions: tuple[Intent, ...]
    visible: tuple[Intent, ...]

    def for_target(self, target: str) -> Intent | None:
        return next((intent for intent in self.visible if intent.target == target), None)


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
    runs: dict[str, _Run] = Field(default_factory=dict)
    admissions: dict[str, Admission] = Field(default_factory=dict)
    queue: list[_Queued] = Field(default_factory=list)


def _finite(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("time must be a finite UTC number")
    return float(value)


def _run_id(identity: str) -> str:
    return "run-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


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
    """Single-owner mutable domain state, with pure immutable observations."""

    def __init__(self) -> None:
        self._state = _State()

    def set_scene(self, scene: Scene) -> None:
        self._state.scenes[scene.scene_id] = scene

    def set_program(self, program: Program) -> None:
        if program.scene_id not in self._state.scenes:
            raise ValueError(f"unknown Scene: {program.scene_id}")
        self._state.programs[program.program_id] = program
        if self._state.now is not None and program.ends_at <= self._state.now:
            self._state.admissions.setdefault(program.activation_id, Admission(
                activation_id=program.activation_id, status="expired", reason="missed_window"
            ))

    def remove_program(self, program_id: str, now: float) -> RuntimeView:
        now = _finite(now)
        if self._state.now is not None and now < self._state.now:
            raise ValueError("Runtime cannot move backwards in UTC")
        program = self._state.programs.get(program_id)
        if program is not None and program.starts_at < now:
            # Reconcile any intervening start before removing its definition;
            # the removal itself is a stop event, before same-time new work.
            if program.ends_at > now:
                self._state.programs[program_id] = program.model_copy(update={"ends_at": now})
        else:
            self._state.programs.pop(program_id, None)
        for run in self._state.runs.values():
            if run.program_id == program_id and run.active:
                run.scheduled_finish_at = min(run.scheduled_finish_at or now, now)
        view = self.advance(now)
        self._state.programs.pop(program_id, None)
        return view

    def export_state(self) -> dict:
        return self._state.model_dump(mode="json")

    @classmethod
    def restore(cls, state: dict) -> Runtime:
        runtime = cls()
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

    def project(self, now: float, *, max_events: int = 10000) -> RuntimeView:
        return self.restore(self.export_state()).advance(now, max_events=max_events)

    def timeline(
        self, start: float, end: float, *, max_events: int = 10000
    ) -> tuple[RuntimeView, ...]:
        """Pure settled views at start and every discrete boundary in [start, end)."""
        start, end = _finite(start), _finite(end)
        if end <= start:
            raise ValueError("timeline end must follow start")
        budget = _TransitionBudget(max_events)
        projected = self.restore(self.export_state())
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
        if activation_id.startswith("program:"):
            raise ValueError("program activation identities are reserved for scheduled ingress")
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
                result = Admission(
                    activation_id=activation_id, status="rejected", reason=denial
                )
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

    def _advance(self, now: float, budget: _TransitionBudget) -> RuntimeView:
        now = _finite(now)
        if self._state.now is not None and now < self._state.now:
            raise ValueError("Runtime cannot move backwards in UTC")
        if self._state.now is None:
            # A cold authority does not replay completely missed Program windows.
            for program in self._state.programs.values():
                if program.ends_at <= now:
                    self._state.admissions[program.activation_id] = Admission(
                        activation_id=program.activation_id, status="expired", reason="missed_window"
                    )
        while True:
            self._skip_leaf_cycles(now)
            event = self._next_event()
            if event is None or event > now:
                break
            budget.consume()
            self._process(event, budget)
        self._state.now = now
        return self._view(now)

    def _matching(self, scene_id: str) -> list[_Run]:
        return [
            run for run in self._state.runs.values()
            if run.parent_id is None and run.active and run.scene.scene_id == scene_id
        ]

    def _protected_conflict(
        self, scene: Scene, priority: int, force: bool, excluded: set[str] | None = None
    ) -> str | None:
        frames = {target for target in scene.participants if target.startswith("frame:")}
        for run in self._state.runs.values():
            if run.parent_id is not None or not run.active or run.run_id in (excluded or set()):
                continue
            if not frames.intersection(run.scene.participants):
                continue
            if frames.intersection(run.scene.protected_frames) and not force:
                return "protected_frames"
            if scene.protected_frames.intersection(run.scene.participants) and run.priority > priority:
                return "protection_not_visible"
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
        boundaries = [
            p.starts_at for p in self._state.programs.values()
            if p.activation_id not in self._state.admissions
        ]
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
        events = [
            p.starts_at for p in self._state.programs.values()
            if p.activation_id not in self._state.admissions
        ]
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
            if not self._matching(item.scene.scene_id) and not self._protected_conflict(
                item.scene, item.priority, item.force
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
        for program in sorted(self._state.programs.values(), key=lambda p: (p.starts_at, p.program_id)):
            if program.starts_at > at or program.activation_id in self._state.admissions:
                continue
            budget.consume()
            scene = self._state.scenes[program.scene_id]
            denial = self._protected_conflict(scene, program.priority, False)
            result = (
                Admission(activation_id=program.activation_id, status="rejected", reason=denial)
                if denial else self._admit(
                    scene, program.activation_id, program.starts_at, program.priority,
                    program_id=program.program_id, program_end=program.ends_at,
                )
            )
            self._state.admissions[program.activation_id] = result
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

    def _view(self, now: float) -> RuntimeView:
        intents: list[Intent] = []
        runs = sorted(self._state.runs.values(), key=lambda r: r.order)
        for run in runs:
            if not run.active or (run.phase == "body" and run.body_done_at is not None):
                continue
            start = run.outro_started_at if run.phase == "outro" else run.cycle_start
            end = start + (run.scene.outro_seconds if run.phase == "outro" else run.scene.cycle_seconds)
            definitions = run.scene.outro_contributions if run.phase == "outro" else run.scene.contributions
            position = max(0.0, now - start)
            for contribution in definitions:
                opacity = contribution.opacity
                if contribution.fade_in_seconds:
                    opacity *= min(1.0, position / contribution.fade_in_seconds)
                if contribution.fade_out_seconds:
                    opacity *= min(1.0, max(0.0, end - now) / contribution.fade_out_seconds)
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
                    fade_out_seconds=contribution.fade_out_seconds,
                    actuator_value=value, phase=run.phase,
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
                ended_at=r.ended_at,
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
