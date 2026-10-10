"""The judge sees a dead Player (`app_absent`) and one running out of open files
(`app_resource_exhausted`) from the broker's run facts (1b P1b; `appliance.kernel.app_facts`)."""
from uuid import uuid4

from appliance.apps.probe import SHIPPED_TIMING, AppRunKey
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.feed import FeedEvent
from appliance.health.judge import (
    APP_ABSENT,
    APP_RESOURCE_EXHAUSTED,
    HealthJudge,
    descriptor_pressure,
)
from appliance.kernel.app_facts import APP_DESCRIPTORS, APP_EXITED, APP_STARTED
from contracts.node_faults import FAULTS

RUN = AppRunKey(uuid4(), 101, 7, 1).document()
NEXT_RUN = AppRunKey(uuid4(), 202, 9, 2).document()
RAISE = FAULTS[APP_ABSENT].raise_window_ms  # 5,000 ms
HOLD = FAULTS[APP_RESOURCE_EXHAUSTED].clear_hold_ms  # 60,000 ms


def judge() -> HealthJudge:
    return HealthJudge(period_ms=SHIPPED_TIMING.period_ms, miss_limit=SHIPPED_TIMING.miss_limit,
                       startup_ms=SHIPPED_TIMING.startup_ms,
                       kill_after_ms=SHIPPED_TIMING.kill_after_ms,
                       pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)


def fact(kind, run=RUN, **value):
    return FeedEvent(0, kind, {"run": run, **value}, "node")


def descriptors(open_files, run=RUN, soft_limit=1024):
    return fact(APP_DESCRIPTORS, run, open=open_files, soft_limit=soft_limit)


def states(subject, now):
    return [(c.code, c.state) for c in subject.verdict(now).conditions]


def connected(subject, now):
    subject.observe_outputs([{"output_id": "HDMI-A-1", "connected": True, "admitted": None}], now)


def test_app_exit_raises_app_absent_until_a_new_run_starts():
    subject = judge()
    connected(subject, 0)
    subject.observe(fact(APP_EXITED), 1000)
    assert states(subject, 1000) == [(APP_ABSENT, "pending")]
    assert [i.tint for i in subject.instructions(1000 + RAISE - 1)] == [False]
    assert states(subject, 1000 + RAISE) == [(APP_ABSENT, "raised")]
    (card,) = subject.instructions(1000 + RAISE)
    assert card.tint and card.lines == ("Photos paused — the player stopped",
                                        f"{APP_ABSENT} · Output HDMI-A-1")
    subject.observe(fact(APP_STARTED), 7000)  # the exited run itself: nothing changes
    assert states(subject, 7000) == [(APP_ABSENT, "raised")]
    subject.observe(fact(APP_STARTED, NEXT_RUN), 8000)
    assert states(subject, 8000) == []
    assert [i.tint for i in subject.instructions(8000)] == [False]
    assert [(t.code, t.state, t.reason) for t in subject.transitions()] == [
        (APP_ABSENT, "pending", "app_exited"), (APP_ABSENT, "raised", "window_elapsed"),
        (APP_ABSENT, "cleared", "started")]


def test_a_relaunch_inside_the_window_is_never_shown():
    subject = judge()
    connected(subject, 0)
    subject.observe(fact(APP_EXITED), 1000)
    subject.observe(fact(APP_STARTED, NEXT_RUN), 1000 + RAISE - 1)
    assert states(subject, 1000 + RAISE) == []
    assert [i.tint for i in subject.instructions(1000 + RAISE)] == [False]
    assert [(t.state, t.reason) for t in subject.transitions()] == [
        ("pending", "app_exited"), ("withdrawn", "started")]


def test_a_feed_gap_keeps_a_raised_app_absent():
    subject = judge()
    subject.observe(fact(APP_EXITED), 0)
    assert states(subject, RAISE) == [(APP_ABSENT, "raised")]
    subject.forget(RAISE + 1)
    assert states(subject, RAISE + 100_000) == [(APP_ABSENT, "raised")]


def test_descriptor_pressure_raises_and_clears_resource_exhausted():
    subject = judge()
    connected(subject, 0)
    subject.observe(descriptors(819), 0)  # 79.98 % of 1,024
    assert states(subject, 0) == []
    subject.observe(descriptors(820), 1000)  # 80.08 %: raised at once
    assert states(subject, 1000) == [(APP_RESOURCE_EXHAUSTED, "raised")]
    (card,) = subject.instructions(1000)
    assert not card.tint  # degraded: content still shows
    subject.observe(descriptors(500), 2000)  # relief starts the clear hold
    subject.observe(descriptors(900), 2000 + HOLD - 1)  # pressure again: the hold restarts
    subject.observe(descriptors(500), 3000 + HOLD)
    assert states(subject, 3000 + 2 * HOLD - 1) == [(APP_RESOURCE_EXHAUSTED, "raised")]
    assert states(subject, 3000 + 2 * HOLD) == []
    assert [(t.state, t.reason) for t in subject.transitions()] == [
        ("raised", "descriptor_pressure"), ("cleared", "descriptor_relief")]
    # Another raise clears with its run's exit; another run's samples never touch it.
    subject.observe(descriptors(1000), 200_000)
    subject.observe(descriptors(10, NEXT_RUN), 200_001)
    assert states(subject, 200_000 + 2 * HOLD) == [(APP_RESOURCE_EXHAUSTED, "raised")]
    subject.observe(fact(APP_EXITED), 400_000)
    assert states(subject, 400_000) == [(APP_ABSENT, "pending")]
    assert [(t.code, t.state, t.reason) for t in subject.transitions()][-1] == (
        APP_RESOURCE_EXHAUSTED, "cleared", "run_changed")


def test_exactly_the_pressure_share_is_pressure():
    """80 % of 1,024 is not a whole count; at a limit of 1,000, 800 is exactly the share."""
    assert descriptor_pressure(800, 1000) and not descriptor_pressure(799, 1000)
    subject = judge()
    subject.observe(descriptors(800, soft_limit=1000), 0)
    assert states(subject, 0) == [(APP_RESOURCE_EXHAUSTED, "raised")]


def test_a_malformed_descriptor_fact_is_ignored():
    subject = judge()
    for value in ({"open": "900", "soft_limit": 1024}, {"open": 900, "soft_limit": 0},
                  {"open": -1, "soft_limit": 1024}, {"open": 900}):
        subject.observe(FeedEvent(0, APP_DESCRIPTORS, {"run": RUN, **value}, "node"), 0)
    assert states(subject, 0) == []
