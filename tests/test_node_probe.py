"""Probe timing rules for one app run (appliance/node/probe.py): pure, no thread, no socket."""
from uuid import uuid4

from test_node_boot import environment

from appliance.node.broker import RunningApp
from appliance.node.probe import (
    KILL_AFTER_MS,
    MISS_LIMIT,
    PROBE_PERIOD_MS,
    STARTUP_BUDGET_MS,
    AppRunKey,
    ProbeClock,
)
from contracts.node_protocol import NodeProcessIdentity

T = PROBE_PERIOD_MS
RUN = AppRunKey(uuid4(), 4242, 31751781, 1)


def kinds(facts):
    return [fact.kind for fact in facts]


def run_unanswered(clock, start, end, *, late=lambda now: False):
    """Turn every T from start (exclusive) to end (inclusive) with no answer; facts by time."""
    return {now: clock.turn(now, late=late(now)) for now in range(start + T, end + 1, T)}


def test_shipped_constants_are_the_frame_values():
    assert (PROBE_PERIOD_MS, MISS_LIMIT, STARTUP_BUDGET_MS, KILL_AFTER_MS) == (2000, 5, 20000, 35000)


def test_run_key_is_taken_from_a_running_app_by_attribute():
    invocation = uuid4()
    running = RunningApp(environment("a"), NodeProcessIdentity(396, 31751781, invocation), 3, uuid4())
    key = AppRunKey.of(running)
    assert key == AppRunKey(invocation, 396, 31751781, 3)
    assert key.document() == {"invocation_id": str(invocation), "pid": 396,
                              "start_ticks": 31751781, "app_epoch": 3}


def test_healthy_run_answered_every_period_reports_nothing():
    clock = ProbeClock(RUN, 0)
    for turn in range(1, 60):
        now = turn * T
        assert clock.turn(now, late=False) == ()
        nonce = f"{turn:064x}"
        clock.sent(nonce, now)
        assert clock.answered(nonce, now + 15)
        assert clock.last_rtt_ms == 15


def test_stale_nonce_is_not_an_answer():
    clock = ProbeClock(RUN, 0)
    first, second = "a" * 64, "b" * 64
    clock.sent(first, 2000)
    assert not clock.answered("c" * 64, 2010)  # never sent
    clock.sent(second, 4000)
    assert clock.answered(second, 4010)
    assert not clock.answered(first, 4020)  # older than the answered one
    assert not clock.answered(second, 4030)  # already answered
    other = ProbeClock(AppRunKey(uuid4(), 1, 2, 1), 0)
    assert not other.answered(second, 4040)  # another run's nonce
    # Stale answers between turns never reset the unanswered time: kill due still comes.
    due = []
    for now in range(4000 + T, 4000 + KILL_AFTER_MS + 2 * T, T):
        assert not clock.answered(first, now - 1)
        assert not clock.answered("d" * 64, now - 1)
        due += [now for fact in clock.turn(now, late=False) if fact.kind == "probe_kill_due"]
    assert due == [40000]  # the first turn at least K after the last real answer (4010)


def test_no_unanswered_fact_before_the_startup_budget():
    clock = ProbeClock(RUN, 10000)
    facts = run_unanswered(clock, 10000, 10000 + STARTUP_BUDGET_MS + 2 * T)
    before = [now for now, got in facts.items() if got and now < 10000 + STARTUP_BUDGET_MS]
    assert before == []
    at_budget = facts[10000 + STARTUP_BUDGET_MS]
    assert kinds(at_budget) == ["probe_unanswered"]
    assert at_budget[0].value == {"run": RUN.document(), "unanswered_ms": STARTUP_BUDGET_MS,
                                  "misses": STARTUP_BUDGET_MS // T}


def test_no_channel_since_launch_is_kill_due_once_after_k():
    clock = ProbeClock(RUN, 0)
    facts = run_unanswered(clock, 0, KILL_AFTER_MS + 6 * T)
    due = [now for now, got in facts.items() if "probe_kill_due" in kinds(got)]
    assert due == [-(-KILL_AFTER_MS // T) * T]  # the first turn at or past K
    [fact] = [f for f in facts[due[0]] if f.kind == "probe_kill_due"]
    assert fact.value == {"run": RUN.document(), "unanswered_ms": due[0]}
    # probe_unanswered every T from the budget on.
    assert all("probe_unanswered" in kinds(got) for now, got in facts.items() if now >= STARTUP_BUDGET_MS)


def test_after_k_counted_misses_unanswered_every_period_until_answered():
    clock = ProbeClock(RUN, 0)
    clock.sent("a" * 64, STARTUP_BUDGET_MS)
    assert clock.answered("a" * 64, STARTUP_BUDGET_MS + 5)
    start = STARTUP_BUDGET_MS + 5
    reported = []
    # The first turn after an answer judges the interval that held it: not a miss.
    for turn in range(1, MISS_LIMIT + 5):
        got = clock.turn(start + turn * T, late=False)
        reported.append(bool(got))
        if got:
            assert got[0].value["misses"] == turn - 1
            assert got[0].value["unanswered_ms"] == turn * T
    assert reported == [False] * MISS_LIMIT + [True] * 4
    clock.sent("b" * 64, start + 9 * T)
    assert clock.answered("b" * 64, start + 9 * T + 5)
    assert clock.turn(start + 10 * T, late=False) == ()


def test_late_intervals_are_not_counted():
    clock = ProbeClock(RUN, 0)
    # Every other turn ran late: only half the time is counted, so no kill at K.
    facts = run_unanswered(clock, 0, 2 * KILL_AFTER_MS - 2 * T, late=lambda now: now % (2 * T) == 0)
    assert "probe_kill_due" not in [kind for got in facts.values() for kind in kinds(got)]
    late_only = ProbeClock(RUN, 0)
    facts = run_unanswered(late_only, 0, 3 * KILL_AFTER_MS, late=lambda now: True)
    assert all(got == () for got in facts.values())
    # One very late turn (a suspended broker) is not unanswered time either.
    stalled = ProbeClock(RUN, 0)
    assert stalled.turn(STARTUP_BUDGET_MS + 10 * KILL_AFTER_MS, late=True) == ()
    assert stalled.turn(STARTUP_BUDGET_MS + 10 * KILL_AFTER_MS + T, late=False) == ()


def test_an_answer_rearms_kill_due():
    clock = ProbeClock(RUN, 0)
    facts = run_unanswered(clock, 0, KILL_AFTER_MS + T)
    assert sum("probe_kill_due" in kinds(got) for got in facts.values()) == 1
    clock.sent("a" * 64, KILL_AFTER_MS + T)
    assert clock.answered("a" * 64, KILL_AFTER_MS + T + 1)
    later = run_unanswered(clock, KILL_AFTER_MS + T + 1, 2 * KILL_AFTER_MS + 3 * T)
    assert sum("probe_kill_due" in kinds(got) for got in later.values()) == 1
