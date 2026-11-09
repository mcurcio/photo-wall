"""A Frame black while content is wanted shows the debug overlay saying why.

The judge's rules against its own facts, then the whole path with no hardware: the real Planner,
Plan, Executor, ProbeResponder encoding, ProbeThread, Feed, FeedReader and HealthJudge.
"""
import hashlib
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from support.frame_outputs import frame_output_of
from support.house_time import UTC_HOUSE
from support.packet_pair import packet_pair
from test_planner import binding, candidate, pool, propose, runtime
from test_player_probe_responder import FakeGLib

from appliance.apps.probe import AppRunKey, ProbeTiming
from appliance.apps.probe_channel import ProbeThread
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.feed import Feed, FeedEvent, answer_feed_read
from appliance.health.judge import (
    APP_ABSENT,
    CARD_PRECEDENCE,
    FRAME_BLACK_CODES,
    NOTHING_TO_SHOW,
    HealthJudge,
)
from appliance.health.runner import FeedReader
from appliance.kernel.app_facts import APP_EXITED, APP_FRAMES_BLACK, APP_STARTED
from central.runtime import Runtime
from contracts.models import Commit, Plan, PlayerConfiguration
from contracts.node_app_link import FrameBlackCause, parse_node_probe_open
from contracts.node_faults import FAULTS, FIRST_PHOTO_WINDOW_MS
from contracts.time import ManualClock
from player.cache import Cache
from player.executor import Executor
from player.mainloop import MainLoopDispatcher
from player.probe_responder import ProbeResponder
from player.rendering import RecordingRenderer
from player.show_clock import ShowClock

OUTPUT = "hdmi-1"
RUN_KEY = AppRunKey(uuid4(), 101, 7, 1)
RUN, OTHER = RUN_KEY.document(), AppRunKey(uuid4(), 202, 9, 2).document()
WINDOW = FIRST_PHOTO_WINDOW_MS
FAST = ProbeTiming(period_ms=50, miss_limit=2, startup_ms=150)
NO_CONTENT = "Photos paused — no photo for this Frame"
NOT_STARTED = "Photos paused — waiting for Central to start it"
BLACK_LINES = {FAULTS[code].household_line for code in FRAME_BLACK_CODES.values()}


def judge() -> HealthJudge:
    return HealthJudge(period_ms=FAST.period_ms, miss_limit=FAST.miss_limit, startup_ms=FAST.startup_ms,
                       pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)


def report(*frames, run=RUN):
    return FeedEvent(0, APP_FRAMES_BLACK, {"run": run, "frames": [
        {"output": output, "cause": cause} for output, cause in frames]}, "node")


def display(*, output=OUTPUT, run=RUN, connected=True, power="on", holding=True):
    return [{"output_id": output, "connected": connected, "admitted": run, "holding": holding, "power": power}]


def on_wall(subject, now=0, **kwargs):
    subject.observe_outputs(display(**kwargs), now)


def cards(subject, now):
    return {i.output: i.lines[0].split(" · since")[0] if i.tint else None
            for i in subject.instructions(now, wall_ms=now)}


def transitions(subject):
    return [(t.code, t.state, t.reason) for t in subject.transitions()
            if t.code in FRAME_BLACK_CODES.values()]


def black_card(subject, now):
    """The Output's raised Frame-black code and the time its evidence began (the verdict's age)."""
    verdict = next(v for v in subject.verdict(now).outputs if v.output == OUTPUT)
    return verdict.card, now - verdict.card_age_ms if verdict.card_age_ms is not None else None


# -- the judge -----------------------------------------------------------------------------------


def test_every_cause_has_a_catalogued_code_and_a_place_on_the_card():
    """Mutation probe: remove a cause from FRAME_BLACK_CODES."""
    assert set(FRAME_BLACK_CODES) == set(FrameBlackCause)
    assert all(code in FAULTS and code in CARD_PRECEDENCE for code in FRAME_BLACK_CODES.values())
    assert CARD_PRECEDENCE[-1] == NOTHING_TO_SHOW


def test_a_black_frame_is_silent_until_the_window_passes():
    """Mutation probe: raise window 0 -> tinted before the window (a healthy cold start must not)."""
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 1000)
    assert cards(subject, 1000 + WINDOW - 1) == {OUTPUT: None}
    assert cards(subject, 1000 + WINDOW) == {OUTPUT: "Photos paused — no plan from Central"}
    assert transitions(subject) == [("frame_no_plan", "pending", "frame_black"),
                                    ("frame_no_plan", "raised", "window_elapsed")]


def test_a_cold_start_that_draws_within_the_window_never_tints():
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    subject.observe(report(), WINDOW - 1)
    assert cards(subject, WINDOW * 2) == {OUTPUT: None}
    assert transitions(subject) == [("frame_no_plan", "pending", "frame_black"),
                                    ("frame_no_plan", "withdrawn", "drawing")]


def test_the_next_report_clears_a_raised_card_at_once():
    """Mutation probe: delete the clear on a report that stops naming the Output."""
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_photo")), 0)
    assert cards(subject, WINDOW) == {OUTPUT: "Photos paused — the photo has not arrived"}
    subject.observe(report(), WINDOW + 500)
    assert cards(subject, WINDOW + 500) == {OUTPUT: None}
    assert transitions(subject)[-1] == ("frame_no_photo", "cleared", "drawing")


def test_a_changed_cause_keeps_the_cards_since_and_takes_the_new_code():
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 1000)
    assert black_card(subject, 1000 + WINDOW) == ("frame_no_plan", 1000)
    subject.observe(report((OUTPUT, "not_started")), 2000 + WINDOW)
    assert black_card(subject, 2000 + WINDOW) == ("frame_not_started", 1000)


@pytest.mark.parametrize(("deliberate", "reason"), [
    ({"power": "off"}, "powered_off"),          # Night off and holds
    ({"run": None}, "admission"),               # a not-admitted Output
    ({"connected": False}, "disconnected"),
    ({"run": OTHER}, "another run"),
])
def test_no_card_on_an_output_the_report_does_not_count_for(deliberate, reason):
    """Mutation probe: the judge ignores power off -> the Night off case carries a card."""
    subject = judge()
    on_wall(subject, **deliberate)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    assert cards(subject, WINDOW * 2).get(OUTPUT) not in BLACK_LINES, reason


def test_power_off_clears_a_card_and_power_on_opens_a_fresh_window():
    """Night off is not an error, so its time is no part of the black."""
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    assert cards(subject, WINDOW) == {OUTPUT: "Photos paused — no plan from Central"}
    on_wall(subject, WINDOW + 1, power="off")
    assert cards(subject, WINDOW + 1) == {OUTPUT: None}
    assert transitions(subject)[-1] == ("frame_no_plan", "cleared", "released")
    on_wall(subject, WINDOW + 2, power="on")   # the report still stands: the Frame is still black
    assert cards(subject, WINDOW * 2 + 1) == {OUTPUT: None}
    assert cards(subject, WINDOW * 2 + 2) == {OUTPUT: "Photos paused — no plan from Central"}


def test_a_report_ahead_of_the_admission_counts_when_it_arrives():
    """One report is all there is until the cause changes, so it waits for its Output."""
    subject = judge()
    on_wall(subject, run=None)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    on_wall(subject, 5000, run=RUN)
    assert cards(subject, 5000 + WINDOW) == {OUTPUT: "Photos paused — no plan from Central"}


def test_a_report_for_another_run_is_ignored():
    """Mutation probe: a report for another run counted."""
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    assert cards(subject, WINDOW) == {OUTPUT: "Photos paused — no plan from Central"}
    subject.observe(report(run=OTHER), WINDOW + 1)              # the other run says nothing is black
    assert cards(subject, WINDOW + 1) == {OUTPUT: "Photos paused — no plan from Central"}
    subject.observe(report((OUTPUT, "no_photo"), run=OTHER), WINDOW + 2)
    assert black_card(subject, WINDOW + 2)[0] == "frame_no_plan"


def test_a_new_run_clears_the_old_runs_card():
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    assert cards(subject, WINDOW) == {OUTPUT: "Photos paused — no plan from Central"}
    subject.observe(FeedEvent(0, APP_STARTED, {"run": OTHER}, "node"), WINDOW + 1)
    assert cards(subject, WINDOW + 1) == {OUTPUT: None}
    assert transitions(subject)[-1] == ("frame_no_plan", "cleared", "released")


def test_a_dead_player_replaces_the_card_with_app_absent():
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    assert black_card(subject, WINDOW)[0] == "frame_no_plan"
    subject.observe(FeedEvent(0, APP_EXITED, {"run": RUN}, "node"), WINDOW + 1)
    assert black_card(subject, WINDOW + 1) == (None, None)     # released at once; the exit's window
    assert black_card(subject, WINDOW + 1 + FAULTS[APP_ABSENT].raise_window_ms)[0] == APP_ABSENT


@pytest.mark.parametrize("value", [
    {"run": RUN},                                                       # no frames
    {"run": RUN, "frames": "no"},
    {"run": RUN, "frames": [{"output": OUTPUT}]},
    {"run": RUN, "frames": [{"output": OUTPUT, "cause": "a_future_one"}]},
    {"run": RUN, "frames": [{"output": "", "cause": "no_plan"}]},
    {"run": RUN, "frames": [{"output": OUTPUT, "cause": "no_plan"}, {"output": OUTPUT, "cause": "no_time"}]},
])
def test_a_malformed_report_leaves_the_verdict_unchanged(value):
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    subject.observe(FeedEvent(0, APP_FRAMES_BLACK, value, "node"), WINDOW + 1)
    assert black_card(subject, WINDOW + 1) == ("frame_no_plan", 0)


def test_a_feed_gap_leaves_the_card_and_a_pending_window():
    subject = judge()
    on_wall(subject)
    subject.observe(report((OUTPUT, "no_plan")), 0)
    subject.forget(WINDOW - 1)                         # pending, not withdrawn
    assert cards(subject, WINDOW) == {OUTPUT: "Photos paused — no plan from Central"}
    subject.forget(WINDOW + 1)                         # raised, kept
    assert cards(subject, WINDOW + 1) == {OUTPUT: "Photos paused — no plan from Central"}


# -- the whole path ------------------------------------------------------------------------------


def local_reader(feed):
    reader = FeedReader("broker", Path("/nonexistent/feed.sock"))
    reader._read = lambda request: {"accepted": True, **answer_feed_read(feed, request)}
    return reader


class Wall:
    """A Player executing a Planner's projection, answering a broker's probes, judged by health."""

    def __init__(self, tmp_path, projection, *, player_id="player-one"):
        self.clock, self.now_ms = ManualClock(100), 0
        self.show_clock = ShowClock(self.clock.monotonic)
        monotonic = self.clock.monotonic()
        self.show_clock.sample(server_time=projection.now, sent_monotonic=monotonic, received_monotonic=monotonic)
        self.renderer = RecordingRenderer()
        self.executor = Executor(player_id, Cache(tmp_path / "cache", 1 << 20), self.renderer, self.clock,
                                 self.show_clock)
        bound = (binding(),)
        self.executor.accept_configuration(PlayerConfiguration(
            player_id=player_id, authority_epoch=1, configuration_revision=1, bindings=bound,
            enabled_outputs=(OUTPUT,)))
        proposal = projection.for_player(player_id)
        self.plan = Plan(plan_id="plan-1", revision=1, player_id=player_id, authority_epoch=1,
                         issued_at=projection.now, valid_from=projection.now, valid_until=projection.valid_until,
                         bindings=bound, layers=proposal.layers, unfilled=proposal.unfilled)
        self.executor.accept_plan(self.plan)

        self.feed = Feed(512)
        self.probes = ProbeThread(self.feed, clock=lambda: self.now_ms, timing=FAST)
        self.glib = FakeGLib()
        self.player_end, self.broker_end = packet_pair()
        self.responder = ProbeResponder(MainLoopDispatcher(self.glib), on_relink=lambda: None,
                                        frames_black=self.executor.frames_black,
                                        connector=lambda _path: self.player_end)
        self.thread = threading.Thread(target=self.responder._channel, daemon=True)
        self.thread.start()
        parse_node_probe_open(self.broker_end.recv(9000))
        self.feed.append("app_link_accepted", {"run": RUN, "player_id": player_id})
        self.probes.adopt(self.broker_end, RUN_KEY)
        self.probes.publish_run(RUN_KEY)
        self.reader = local_reader(self.feed)
        self.judge = judge()
        self.judge.observe_outputs(display(), 0)
        self.probes.step(self.now_ms)

    def exchange(self):
        """One probe turn: the broker sends, the Player answers on its queue, the broker reads."""
        self.now_ms += FAST.period_ms
        self.probes.step(self.now_ms)
        deadline = time.monotonic() + 5
        while not self.glib.callbacks and time.monotonic() < deadline:
            time.sleep(.002)
        self.glib.run_idle()
        for _ in range(4):                      # one packet a pass: the answer, then the report
            self.probes.step(self.now_ms)
        self.reader.drain(self.judge, lambda: self.now_ms)

    def secure_and_prepare(self):
        for layer in self.plan.layers:
            data = b"photo-" + layer.assignment_id.encode()
            assert self.executor.acquire(layer.assignment_id, [data], sha256=hashlib.sha256(data).hexdigest(),
                                         size=len(data))
        self.executor.prepare_imminent()

    def card(self, after_ms):
        self.now_ms += after_ms
        return self.judge.instructions(self.now_ms, wall_ms=3_600_000 + self.now_ms)

    def close(self):
        self.probes.close()


@pytest.fixture
def walls(tmp_path):
    made = []

    def build(projection, **kwargs):
        directory = tmp_path / str(len(made))
        directory.mkdir()
        wall = Wall(directory, projection, **kwargs)
        made.append(wall)
        return wall

    yield build
    for wall in made:
        wall.close()


def unfilled_frames():
    return {"no photo fits": dict(assets=[candidate("wide", width=1920, height=1080)]),
            "the Source is empty": dict(pools={"holiday:1": pool()}),
            "the screen is unknown": dict(frame_outputs={"left": frame_output_of("left", None)})}


@pytest.mark.parametrize("case", list(unfilled_frames()))
def test_a_frame_with_no_photo_it_can_show_says_so(walls, case):
    """Mutation probes: `ProbeThread` drops the packet; spans recorded only where the wall
    assignment left a slot unfilled."""
    projection = propose(runtime(), **unfilled_frames()[case])
    wall = walls(projection)
    assert wall.plan.unfilled and not wall.plan.layers
    wall.exchange()
    assert not wall.card(WINDOW - 1)[0].tint
    (card,) = wall.card(1)
    assert card.tint and card.lines[0].startswith(f"{NO_CONTENT} · since ")
    assert card.lines[1] == f"frame_no_content · Player player-one · Output {OUTPUT}"


def test_a_frame_with_no_active_run_stays_silent(walls):
    """An empty plan for nothing wanted."""
    projection = propose(Runtime(house=UTC_HOUSE))
    wall = walls(projection)
    assert not wall.plan.unfilled
    wall.exchange()
    assert [card.tint for card in wall.card(WINDOW * 2)] == [False]


def test_a_due_photo_with_no_start_grant_says_so_and_clears_when_drawn(walls):
    wall = walls(propose(runtime()))
    wall.secure_and_prepare()
    wall.exchange()
    (card,) = wall.card(WINDOW)
    assert card.tint and card.lines[0].startswith(f"{NOT_STARTED} · since ")
    readiness = wall.executor.readiness()
    wall.executor.accept_commit(Commit(plan_id=wall.plan.plan_id, revision=1, authority_epoch=1,
                                       readiness_sequence=readiness.sequence,
                                       assignment_ids=readiness.prepared, committed_at=wall.show_clock.now()))
    wall.executor.tick()
    wall.exchange()
    assert [card.tint for card in wall.card(0)] == [False]


def test_the_report_goes_only_on_a_change(walls):
    """Sixty probes with no change put one report on the feed."""
    wall = walls(propose(runtime(), pools={"holiday:1": pool()}))
    for _ in range(60):
        wall.exchange()
    reports = [event for event in wall.feed.read(0, incarnation=None, limit=500).events
               if event.kind == APP_FRAMES_BLACK]
    assert len(reports) == 1
