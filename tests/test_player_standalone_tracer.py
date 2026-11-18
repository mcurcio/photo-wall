"""The tracer of decision 0032 (slice B): a Player keeps showing photos while Central answers 503.

Real Coordinator over PostgreSQL, one Player with two Frames, one Scene at 3 s a photo, a pool of
12 prepared stills (fewer than the two Frames' offered windows hold, so a photo is offered on both
within one window and the continuation lists must keep it to one); the real Executor with a RecordingRenderer and a real Cache. A delivery shim
stands for the Player's State route: it answers 503 (raises, delivers nothing) between t=30 s and
t=90 s. The executor ticks every 0.5 s of manual time.

Owner, 2026-11-15: "Them player should fallback to a standalone mode when central is down";
picked (Q1, guidance): the Frame cycles "its own next photos" from Central's plan, "never a photo
another Frame is using". Owner, 2026-10-11: "Never show the same photo on two frames at the same
time".

- T1 photos keep changing: across t in [30, 90) the drawn photo on each Frame changes at every
  3 s turn, also past the last plan's `valid_until`.
- T2 never on two Frames: at no tick does one asset show on both Frames.
- T3 no card: `frames_black()` is empty throughout, once both Frames first show a photo (before
  that, the Run's first turn waits for its grant: "not started" is the truth then).
- T4 rejoin with no black: from t=90 every acknowledged composition on each Frame has a visible
  layer until the first layer of the new plan is drawn there.
"""
from __future__ import annotations

import hashlib
import math

import pytest
from support.measured_photos import measure_all
from support.photo_pools import photo, seed_pool
from support.position import PORTRAIT_ON_WIDE, save_position
from test_coordination import prepare
from test_registry import enroll, frame

from central.coordination import CoordinationError, CoordinationLimits, Coordinator
from central.runtime import Contribution, Scene
from contracts.continuation import CONTINUATION_PREFIX
from player.cache import Cache
from player.executor import AuthorityError, Executor, may_still_play
from player.rendering import RecordingRenderer
from player.show_clock import ShowClock
from player.visibility import visible_now

pytestmark = pytest.mark.db

OUTPUTS = ("HDMI-A-1", "HDMI-A-2")
TURN = 3.0
OUTAGE = (30.0, 90.0)
END = 120.0
TICK = 0.5


def _bytes(asset_id: str) -> bytes:
    return f"tracer:{asset_id}".encode()


class Wall:
    """Central, the Player and the shim between them."""

    def __init__(self, registry, tmp_path):
        self.registry, self.clock = registry, registry.clock
        identity, _, _ = enroll(registry, count=2)
        self.player_id, self.epoch = identity["player_id"], identity["authority_epoch"]
        for number, output in enumerate(OUTPUTS):
            frame(registry, f"frame-{number}")
            registry.bind(f"frame-{number}", self.player_id, output, expected_generation=0, by="console")
            save_position(registry, f"frame-{number}", PORTRAIT_ON_WIDE)
        portraits = tuple(photo(f"still-{n:02}", taken_at=900 + n, width=2400, height=3600)
                          for n in range(12))
        seed_pool(registry, "library:1", portraits)
        measure_all(registry, portraits)
        prepare(registry, portraits)
        # Windows of the plan line up with Central's renewal, so the last plan before the
        # outage runs out within it (`valid_until` at most ~20 s past t=30).
        self.clock.advance(math.ceil(self.clock.utc() / 30) * 30 - self.clock.utc())
        self.t0 = self.clock.utc()
        self.coordinator = Coordinator(registry.db, self.clock,
                                       CoordinationLimits(horizon_seconds=20, plan_seconds=120))
        # A cut, not a cross-fade: a Scene's Transition leaves PREPARE_SECONDS (5 s) of each
        # turn free of a dissolve (central/runtime.py `Scene.check_transition`), so a 3 s turn
        # takes none.
        self.coordinator.runtime.command("set_scene", Scene(
            scene_id="tracer", loop=True, cycle_seconds=TURN,
            contributions=tuple(Contribution(target=f"frame:frame-{n}", kind="media",
                                             source_refs=("library:1",)) for n in range(2))),
            by="console")
        self.coordinator.runtime.command("activate", "tracer", "tracer-now", self.t0,
                                         by="console")
        self.show_clock = ShowClock(self.clock.monotonic)
        self.renderer = RecordingRenderer(capacity=8)
        self.cache = Cache(tmp_path / "cache", 1024 ** 2)
        self.executor = Executor(self.player_id, self.cache, self.renderer, self.clock,
                                 self.show_clock)
        self.last_central = -math.inf

    @property
    def t(self) -> float:
        return self.clock.utc() - self.t0

    def down(self) -> bool:
        return OUTAGE[0] <= self.t < OUTAGE[1]

    def central(self) -> None:
        """Central's scheduler, once a second, outage or not (it is slow, not stopped)."""
        if self.clock.utc() - self.last_central >= 1:
            self.coordinator.advance()
            self.last_central = self.clock.utc()

    def deliver(self) -> None:
        """The State route: the configuration, the plan and its grants, with Central's time."""
        if self.down():
            return          # 503: nothing delivered, nothing reported
        delivery = self.coordinator.delivery(self.player_id, self.epoch)
        monotonic = self.clock.monotonic()
        self.show_clock.sample(server_time=self.clock.utc(), sent_monotonic=monotonic,
                               received_monotonic=monotonic)
        self.executor.accept_configuration(delivery["configuration"])
        if delivery["plan"] is not None:
            self.executor.accept_plan(delivery["plan"])
        for commit in delivery["commits"]:
            try:
                self.executor.accept_commit(commit)
            except AuthorityError:
                pass        # a grant of another readiness: the next report renews it

    def download(self) -> None:
        """One media job per tick (downloads take time), the earliest layer first."""
        plan = self.executor._plan
        if plan is None or self.down():
            return
        secured = self.executor.secured_ids()
        now = self.show_clock.now()
        jobs = sorted((layer for layer in plan.layers if layer.variant is not None
                       and layer.assignment_id not in secured
                       and may_still_play(plan, layer, now)), key=lambda layer: layer.start)
        if jobs:
            layer = jobs[0]
            data = _bytes(layer.variant.asset_id)
            if not self.executor.resolve_cached(layer.assignment_id):
                self.executor.acquire(layer.assignment_id, [data],
                                      sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def report(self) -> None:
        if self.down() or self.executor._plan is None:
            return
        readiness = self.executor.readiness()
        if readiness is not None:
            try:
                self.coordinator.readiness(self.player_id, readiness)
            except CoordinationError:
                pass        # an offer Central no longer holds: the next plan is fetched

    def step(self) -> dict[str, tuple]:
        self.central()
        self.deliver()
        self.download()
        self.executor.prepare_imminent()
        self.executor.tick()
        self.report()
        drawn = {}
        for output in OUTPUTS:
            composition = self.renderer.outputs.get(output)
            drawn[output] = () if composition is None else tuple(visible_now(composition.layers))
        return drawn


def test_a_player_cut_off_from_central_keeps_cycling_and_rejoins_with_no_black(registry,
                                                                              tmp_path):
    wall = Wall(registry, tmp_path)
    ticks: list[tuple[float, dict[str, tuple], tuple]] = []
    plans_before_outage = []
    held_plans = []
    while wall.t < END:
        drawn = wall.step()
        ticks.append((wall.t, drawn, wall.executor.frames_black()))
        held_plans.append(wall.executor._plan)
        if wall.t < OUTAGE[0] and wall.executor._plan is not None:
            plans_before_outage.append(wall.executor._plan)
        wall.clock.advance(TICK)

    def asset(locals_) -> str | None:
        return locals_[-1].layer.variant.asset_id if locals_ else None

    # T2: one photo is never on both Frames: never drawn on both at once, and never in both
    # Frames' continuation lists of the plan the Player holds (either may cycle to it any time).
    for plan in {id(plan): plan for plan in held_plans if plan is not None}.values():
        layers = {layer.assignment_id: layer for layer in plan.layers}
        listed = {output: {layers[key].variant.asset_id for c in plan.continuations
                           if c.output_id == output for key in c.photos} for output in OUTPUTS}
        assert not listed[OUTPUTS[0]] & listed[OUTPUTS[1]], plan.revision
    for t, drawn, _ in ticks:
        on_first = {local.layer.variant.asset_id for local in drawn[OUTPUTS[0]]}
        on_second = {local.layer.variant.asset_id for local in drawn[OUTPUTS[1]]}
        assert not on_first & on_second, (t, on_first & on_second)

    # T1: each Frame's photo changes at every 3 s turn of the outage, past the plan's validity.
    last_plan = plans_before_outage[-1]
    assert last_plan.valid_until - wall.t0 <= OUTAGE[0] + 20 + TURN, last_plan.valid_until
    assert last_plan.continuations
    for output in OUTPUTS:
        turns = [asset(drawn[output]) for t, drawn, _ in ticks
                 if OUTAGE[0] <= t < OUTAGE[1] and (t % TURN) == 1.0]
        assert len(turns) == (OUTAGE[1] - OUTAGE[0]) / TURN and None not in turns, (output, turns)
        assert all(a != b for a, b in zip(turns, turns[1:])), (output, turns)
        late = [drawn[output][-1].layer.assignment_id for t, drawn, _ in ticks
                if last_plan.valid_until - wall.t0 < t < OUTAGE[1] and drawn[output]]
        assert late and all(key.startswith(CONTINUATION_PREFIX) for key in late)

    # T4: from t=90 each Frame shows a picture at every tick until a layer of the new plan is
    # drawn, and one is drawn.
    for output in OUTPUTS:
        after = [(t, drawn[output]) for t, drawn, _ in ticks if t >= OUTAGE[1]]
        rejoined = next(index for index, (_, locals_) in enumerate(after)
                        if locals_ and not locals_[-1].layer.assignment_id.startswith(
                            CONTINUATION_PREFIX))
        assert all(locals_ for _, locals_ in after[:rejoined + 1]), (output, after[:rejoined + 1])

    # T3: no card once both Frames show a photo.
    shown = next(index for index, (_, drawn, _) in enumerate(ticks) if all(drawn.values()))
    assert ticks[shown][0] < OUTAGE[0]
    assert [(t, black) for t, _, black in ticks[shown:] if black] == []
