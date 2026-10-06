"""Health judge: the fault catalogue, the K rule at construction, and the app_unresponsive rules."""
from types import MappingProxyType
from uuid import uuid4

import pytest

from appliance.apps.probe import SHIPPED_TIMING, AppRunKey
from appliance.display_host.overlay.instruction import (
    MAX_TEXT,
    PULSE_DEADLINE_MS,
    OverlayInstruction,
    PresentedReport,
)
from appliance.feed import FeedEvent
from appliance.health import runner
from appliance.health.judge import (
    APP_UNRESPONSIVE,
    MAX_OUTPUTS,
    RING_CAPACITY,
    HealthJudge,
    OutputVerdict,
    Presented,
)
from contracts import node_faults
from contracts.node_faults import FAULTS, Fault, catalogue_digest

RAISE = FAULTS[APP_UNRESPONSIVE].raise_window_ms
HOLD = FAULTS[APP_UNRESPONSIVE].clear_hold_ms
RUN = AppRunKey(uuid4(), 101, 7, 1).document()
NEXT_RUN = AppRunKey(uuid4(), 202, 9, 2).document()


def timing(**overrides):
    values = {"period_ms": SHIPPED_TIMING.period_ms, "miss_limit": SHIPPED_TIMING.miss_limit,
              "startup_ms": SHIPPED_TIMING.startup_ms, "kill_after_ms": SHIPPED_TIMING.kill_after_ms,
              "pulse_deadline_ms": PULSE_DEADLINE_MS, "catalogue": FAULTS}
    return {**values, **overrides}


def fact(kind, run=RUN, **value):
    return FeedEvent(0, kind, {"run": run, **value}, "node")


def unanswered(run=RUN):
    return fact("probe_unanswered", run, unanswered_ms=12000, misses=6)


def answered(run=RUN):
    return fact("probe_answered", run, rtt_ms=3)


def states(judge, now):
    return [(condition.code, condition.state) for condition in judge.verdict(now).conditions]


# -- catalogue -----------------------------------------------------------------------------


def test_the_catalogue_has_the_one_m1_row():
    assert dict(FAULTS) == {APP_UNRESPONSIVE: Fault(
        APP_UNRESPONSIVE, True, "Photos paused — the player stopped responding", 5000, 10000)}
    with pytest.raises(TypeError):
        FAULTS["other"] = FAULTS[APP_UNRESPONSIVE]


def test_the_catalogue_digest_names_the_rows(monkeypatch):
    digest = catalogue_digest()
    assert len(digest) == 64 and digest == catalogue_digest()
    row = FAULTS[APP_UNRESPONSIVE]
    monkeypatch.setattr(node_faults, "FAULTS", MappingProxyType({APP_UNRESPONSIVE: Fault(
        row.code, row.display_affecting, row.household_line, row.raise_window_ms, 9999)}))
    assert catalogue_digest() != digest


@pytest.mark.parametrize("row", [
    ("App-Unresponsive", True, "line", 1, 1), ("app", 1, "line", 1, 1), ("app", True, "", 1, 1),
    ("app", True, "x" * 97, 1, 1), ("app", True, "line", -1, 1), ("app", True, "line", 1, 1.0),
])
def test_a_malformed_catalogue_row_is_refused(row):
    with pytest.raises(ValueError, match="^fault_catalogue_row$"):
        Fault(*row)


# -- construction and the K rule -----------------------------------------------------------


def test_the_shipped_constants_satisfy_the_k_rule():
    """CI's K-rule check: the judge as released, built from the published constants."""
    judge = runner.shipped_judge()
    assert judge.verdict(0).conditions == ()
    k, t, s = SHIPPED_TIMING.miss_limit, SHIPPED_TIMING.period_ms, SHIPPED_TIMING.startup_ms
    assert SHIPPED_TIMING.kill_after_ms > k * t + RAISE + PULSE_DEADLINE_MS
    assert SHIPPED_TIMING.kill_after_ms > s + RAISE + PULSE_DEADLINE_MS


@pytest.mark.parametrize("kill_after_ms", [25000, 28000])  # S + raise + D = 28000
def test_a_kill_before_the_card_after_startup_refuses_construction(kill_after_ms):
    with pytest.raises(ValueError, match="^k_rule$"):
        HealthJudge(**timing(kill_after_ms=kill_after_ms))
    HealthJudge(**timing(kill_after_ms=28001))


def test_a_kill_before_the_card_after_k_misses_refuses_construction():
    # k·T + raise + D = 10 × 2000 + 5000 + 3000 = 28000 with a short startup budget.
    with pytest.raises(ValueError, match="^k_rule$"):
        HealthJudge(**timing(miss_limit=10, startup_ms=1000, kill_after_ms=28000))
    HealthJudge(**timing(miss_limit=10, startup_ms=1000, kill_after_ms=28001))


def test_the_k_rule_takes_the_raise_window_from_the_catalogue():
    row = FAULTS[APP_UNRESPONSIVE]
    wide = {APP_UNRESPONSIVE: Fault(row.code, True, row.household_line, 12000, row.clear_hold_ms)}
    with pytest.raises(ValueError, match="^k_rule$"):  # 20000 + 12000 + 3000 > 35000
        HealthJudge(**timing(catalogue=wide))


def test_a_catalogue_without_the_code_or_with_a_misnamed_row_is_refused():
    with pytest.raises(ValueError, match="^fault_code_unknown$"):
        HealthJudge(**timing(catalogue={}))
    with pytest.raises(ValueError, match="^fault_catalogue$"):
        HealthJudge(**timing(catalogue={"other": FAULTS[APP_UNRESPONSIVE]}))
    with pytest.raises(ValueError, match="^judge_timing$"):
        HealthJudge(**timing(period_ms=0))


# -- rules ---------------------------------------------------------------------------------


def test_a_healthy_run_has_no_condition():
    judge = HealthJudge(**timing())
    for now in range(0, 60000, 2000):
        judge.observe(answered(), now)
    assert judge.verdict(60000) == judge.verdict(60000)
    assert judge.verdict(60000).conditions == () and judge.verdict(60000).sequence == 0


def test_unanswered_is_pending_until_the_raise_window_has_passed():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 1000)
    assert states(judge, 1000) == [(APP_UNRESPONSIVE, "pending")]
    for now in range(3000, 1000 + RAISE, 2000):
        judge.observe(unanswered(), now)
    assert states(judge, 1000 + RAISE - 1) == [(APP_UNRESPONSIVE, "pending")]
    verdict = judge.verdict(1000 + RAISE)
    assert [(c.state, c.run, c.age_ms) for c in verdict.conditions] == [("raised", RUN, RAISE)]
    assert [(t.state, t.reason) for t in judge.transitions()] == [
        ("pending", "unanswered"), ("raised", "window_elapsed")]
    assert verdict.sequence == 2


def test_an_answer_while_pending_withdraws_it_and_restarts_the_window():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.observe(answered(), RAISE - 1)
    assert states(judge, RAISE) == []
    judge.observe(unanswered(), RAISE + 1000)
    assert states(judge, 2 * RAISE) == [(APP_UNRESPONSIVE, "pending")]
    assert states(judge, 2 * RAISE + 1000) == [(APP_UNRESPONSIVE, "raised")]
    assert [t.state for t in judge.transitions()] == ["pending", "withdrawn", "pending", "raised"]


def raised_judge():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.observe(unanswered(), RAISE)
    assert states(judge, RAISE) == [(APP_UNRESPONSIVE, "raised")]
    return judge


def test_a_raised_condition_clears_only_after_answers_held_for_the_clear_hold():
    judge = raised_judge()
    start = RAISE + 2000
    for now in range(start, start + HOLD, 2000):
        judge.observe(answered(), now)
        assert states(judge, now) == [(APP_UNRESPONSIVE, "raised")]
    assert states(judge, start + HOLD - 1) == [(APP_UNRESPONSIVE, "raised")]
    assert states(judge, start + HOLD) == []
    assert [(t.state, t.reason) for t in judge.transitions()][-1] == ("cleared", "hold_elapsed")


def test_an_unanswered_fact_during_the_hold_keeps_it_raised():
    judge = raised_judge()
    start = RAISE + 2000
    judge.observe(answered(), start)
    judge.observe(unanswered(), start + HOLD - 1000)  # still unanswered: the hold restarts
    assert states(judge, start + HOLD) == [(APP_UNRESPONSIVE, "raised")]
    judge.observe(answered(), start + HOLD + 1000)
    assert states(judge, start + 2 * HOLD + 999) == [(APP_UNRESPONSIVE, "raised")]
    assert states(judge, start + 2 * HOLD + 1000) == []


def test_a_kill_keeps_the_run_raised_and_its_own_answers_never_clear_it():
    judge = raised_judge()
    judge.observe(fact("app_killed", reason="unresponsive", unanswered_ms=35000), 30000)
    judge.observe(answered(), 31000)  # an answer already in flight from the killed run
    assert states(judge, 31000 + 10 * HOLD) == [(APP_UNRESPONSIVE, "raised")]
    # A new run (M3's restart) that answers clears it after the hold.
    judge.observe(answered(NEXT_RUN), 200000)
    assert states(judge, 200000 + HOLD - 1) == [(APP_UNRESPONSIVE, "raised")]
    assert states(judge, 200000 + HOLD) == []


def test_a_kill_raises_at_once_even_unseen_or_pending():
    judge = HealthJudge(**timing())
    judge.observe(fact("app_killed", reason="unresponsive", unanswered_ms=35000), 0)
    assert [(c.state, c.run) for c in judge.verdict(0).conditions] == [("raised", RUN)]
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.observe(fact("app_killed", reason="unresponsive", unanswered_ms=35000), 1)
    assert states(judge, 1) == [(APP_UNRESPONSIVE, "raised")]
    assert judge.transitions()[-1].reason == "app_killed"


def test_a_new_run_does_not_inherit_an_old_runs_window():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.observe(unanswered(NEXT_RUN), RAISE - 1)
    verdict = judge.verdict(RAISE)
    assert [(c.state, c.run) for c in verdict.conditions] == [("pending", NEXT_RUN)]


def test_a_feed_gap_withdraws_pending_and_restarts_a_running_hold_but_keeps_raised():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.forget(1000)
    assert states(judge, 1000 + RAISE) == []
    judge = raised_judge()
    judge.observe(answered(), RAISE + 1000)
    judge.forget(RAISE + 1000 + HOLD - 1)
    assert states(judge, RAISE + 1000 + HOLD) == [(APP_UNRESPONSIVE, "raised")]
    judge.observe(answered(), RAISE + 1000 + HOLD)
    assert states(judge, RAISE + 1000 + 2 * HOLD) == []


@pytest.mark.parametrize("event", [
    fact("probe_channel", state="open"), fact("probe_channel", state="closed"),
    fact("app_link_recorded"), fact("relink_sent"),
    fact("app_link_refused", status=409, reason="central_refused"),
    fact("kill_withheld", reason="recovery_armed"),
    fact("app_killed", reason="other", unanswered_ms=1),
    fact("some_future_kind", detail=1),
    FeedEvent(0, "probe_unanswered", {"run": {"pid": 1}, "unanswered_ms": 1, "misses": 9}, "node"),
    FeedEvent(0, "probe_unanswered", {"unanswered_ms": 1}, "node"),
    FeedEvent(0, "probe_unanswered", {"run": {**RUN, "pid": "1"}}, "node"),
])
def test_other_facts_are_never_a_fault(event):
    judge = HealthJudge(**timing())
    judge.observe(event, 0)
    assert judge.verdict(60000).conditions == () and judge.transitions() == ()


def test_an_accepted_app_link_keeps_the_player_id():
    judge = HealthJudge(**timing())
    judge.observe(fact("app_link_accepted", player_id="player-1"), 0)
    assert judge.player == (RUN, "player-1") and judge.verdict(0).conditions == ()


def test_the_transition_ring_is_bounded_and_counts_what_it_drops():
    judge = HealthJudge(**timing())
    for index in range(RING_CAPACITY):  # pending + withdrawn per round
        judge.observe(unanswered(), 10 * index)
        judge.observe(answered(), 10 * index + 1)
    assert len(judge.transitions()) == RING_CAPACITY and judge.ring_dropped == RING_CAPACITY
    assert judge.transitions()[-1].sequence == judge.verdict(10 * RING_CAPACITY).sequence
    assert judge.verdict(0).sequence == 2 * RING_CAPACITY


def test_the_judge_clock_never_runs_backwards():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), RAISE)
    assert states(judge, 0) == [(APP_UNRESPONSIVE, "pending")]
    with pytest.raises(ValueError, match="^judge_clock$"):
        judge.verdict(1.5)


# -- per Output: the display snapshot, underlay and the overlay projection -----------------

LINE = FAULTS[APP_UNRESPONSIVE].household_line
OFF = ("", "")


def output(name="Virtual-1", run=RUN, *, connected=True, fault=None):
    """One entry of Display's `outputs` snapshot (appliance/display_host/runner.py)."""
    admitted = None if run is None else {**run, "frame_id": "frame-1"}
    return {"output_id": name, "connected": connected, "admitted": admitted,
            "diagnostic": "released" if run else "presented", "fault": fault}


def underlays(judge, now):
    return [(o.output, o.underlay, o.codes) for o in judge.verdict(now).outputs]


def cards(judge, now):
    return [(i.output, i.tint, i.lines) for i in judge.instructions(now)]


def test_each_connected_output_of_the_snapshot_is_live_or_slate_while_healthy():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(), output("HDMI-A-2", None),
                           output("HDMI-A-3", NEXT_RUN, connected=False)], 0)
    assert underlays(judge, 0) == [("HDMI-A-2", "slate", ()), ("Virtual-1", "live", ())]
    assert cards(judge, 0) == [("HDMI-A-2", False, OFF), ("Virtual-1", False, OFF)]


def test_the_underlay_is_taken_from_the_admission_never_from_the_fault():
    judge = HealthJudge(**timing())  # E-B10a-7: an admitted Output still shows `app_absent`
    judge.observe_outputs([output(fault="app_absent")], 0)
    assert underlays(judge, 0) == [("Virtual-1", "live", ())]


def test_the_admitted_unresponsive_run_is_held_and_every_output_shows_the_code():
    judge = HealthJudge(**timing())
    judge.observe(fact("app_link_accepted", player_id="player-1"), 0)
    judge.observe_outputs([output(), output("HDMI-A-2", None), output("HDMI-A-3", NEXT_RUN)], 0)
    judge.observe(unanswered(), 0)
    assert underlays(judge, RAISE - 1) == [  # pending is not shown: still live
        ("HDMI-A-2", "slate", ()), ("HDMI-A-3", "live", ()), ("Virtual-1", "live", ())]
    raised = (APP_UNRESPONSIVE,)
    assert underlays(judge, RAISE) == [("HDMI-A-2", "slate", raised),
                                       ("HDMI-A-3", "live", raised),
                                       ("Virtual-1", "held", raised)]
    assert cards(judge, RAISE)[-1] == ("Virtual-1", True, (
        LINE, "app_unresponsive · Player player-1 · Output Virtual-1"))
    assert all(tint for _, tint, _ in cards(judge, RAISE))


def test_after_the_kill_the_slate_keeps_the_card():
    judge = raised_judge()
    judge.observe_outputs([output()], RAISE)
    judge.observe(fact("app_killed", reason="unresponsive", unanswered_ms=35000), 30000)
    judge.observe_outputs([output(run=None, fault="surface_lease_or_process_lost")], 30001)
    assert underlays(judge, 30001) == [("Virtual-1", "slate", (APP_UNRESPONSIVE,))]
    assert cards(judge, 30001) == [("Virtual-1", True, (
        LINE, "app_unresponsive · Output Virtual-1"))]  # no accepted link seen: no Player id


def test_the_serial_bumps_on_every_change_of_an_outputs_card_and_only_then():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(), output("HDMI-A-2", None)], 0)
    healthy = judge.instructions(0)
    assert judge.instructions(1) == healthy  # unchanged: same serials
    assert len({i.serial for i in healthy}) == 2
    judge.observe(unanswered(), 1)
    judge.observe(unanswered(), 1 + RAISE)
    tinted = judge.instructions(1 + RAISE)
    assert len(tinted) == 2
    assert all(new.tint and new.serial > max(i.serial for i in healthy) for new in tinted)
    start = RAISE + 2001
    for now in range(start, start + HOLD + 1, 2000):
        judge.observe(answered(), now)
    cleared = judge.instructions(start + HOLD)
    assert [(i.tint, i.lines) for i in cleared] == [(False, OFF)] * 2
    assert all(new.serial > max(i.serial for i in tinted) for new in cleared)
    judge.observe(fact("app_link_accepted", player_id="player-2"), start + HOLD)
    assert judge.instructions(start + HOLD + 1) == cleared  # lines unchanged while off


def test_a_new_player_id_changes_the_card_and_its_serial():
    judge = raised_judge()
    judge.observe_outputs([output()], RAISE)
    before = judge.instructions(RAISE)[0]
    judge.observe(fact("app_link_accepted", player_id="player-9"), RAISE)
    after = judge.instructions(RAISE)[0]
    assert after.serial > before.serial and "Player player-9" in after.lines[1]


def test_an_invalidation_refines_the_snapshot_until_the_next_one():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(), output("HDMI-A-2")], 0)
    invalidated = FeedEvent(9, "SurfaceFact", {"output": {"output_id": "Virtual-1"},
                                               "state": "invalidated"}, "node")
    judge.observe_display(invalidated, 1)
    assert underlays(judge, 1) == [("HDMI-A-2", "live", ()), ("Virtual-1", "slate", ())]
    for other in (FeedEvent(10, "CompositorPresentation", {"fact": {}}, "node"),
                  FeedEvent(11, "SurfaceFact", {"output": {"output_id": "Virtual-1"},
                                                "state": "presented_to_compositor"}, "node"),
                  FeedEvent(12, "SurfaceFact", {"output": {"output_id": "DP-9"},
                                                "state": "invalidated"}, "node")):
        judge.observe_display(other, 2)
    assert underlays(judge, 2) == [("HDMI-A-2", "live", ()), ("Virtual-1", "slate", ())]
    judge.observe_outputs([output(), output("HDMI-A-2")], 3)
    assert underlays(judge, 3) == [("HDMI-A-2", "live", ()), ("Virtual-1", "live", ())]


def test_the_verdict_sequence_bumps_when_an_outputs_verdict_changes():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    assert judge.verdict(0).sequence == 1
    judge.observe_outputs([output()], 1)  # same verdict
    assert judge.verdict(1).sequence == 1
    judge.observe_outputs([output(run=None)], 2)
    assert judge.verdict(2).sequence == 2 and judge.transitions() == ()


@pytest.mark.parametrize("snapshot", [
    None, {}, "x", [1], [{"output_id": "", "connected": True, "admitted": None}],
    [{"output_id": "V", "admitted": None}],
    [{"output_id": "V", "connected": 1, "admitted": None}],
    [{"output_id": "V", "connected": True, "admitted": {"pid": 1}}],
    [{"output_id": "V", "connected": True, "admitted": {**RUN, "pid": "101"}}],
    [{"output_id": "V", "connected": True, "admitted": None}] * 2,
    [{"output_id": f"V{index}", "connected": True, "admitted": None}
     for index in range(MAX_OUTPUTS + 1)],
])
def test_a_malformed_snapshot_is_refused_and_changes_nothing(snapshot):
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    with pytest.raises(ValueError, match="^display_snapshot$"):
        judge.observe_outputs(snapshot, 1)
    assert underlays(judge, 1) == [("Virtual-1", "live", ())]


def test_an_output_name_the_health_layer_cannot_carry_is_not_projected():
    judge = raised_judge()
    long = "O" * (MAX_TEXT + 1)
    judge.observe(fact("app_link_accepted", player_id="p" * 80), RAISE)
    judge.observe_outputs([output(long), output("Virtual-1")], RAISE)
    assert [o.output for o in judge.verdict(RAISE).outputs] == [long, "Virtual-1"]
    [instruction] = judge.instructions(RAISE)
    assert instruction.output == "Virtual-1" and len(instruction.lines[1]) == MAX_TEXT


def test_a_presented_serial_is_kept_in_the_ring_once():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    [instruction] = judge.instructions(0)
    assert judge.presented(PresentedReport("Virtual-1", instruction.serial), 5) is True
    assert judge.presented(PresentedReport("Virtual-1", instruction.serial), 6) is False
    assert judge.presented(PresentedReport("Virtual-1", instruction.serial + 1), 6) is False
    assert judge.presented(PresentedReport("HDMI-A-2", instruction.serial), 6) is False
    assert judge.transitions() == (Presented(judge.sequence, "Virtual-1", instruction.serial, 5),)


def test_the_projection_is_displays_instruction_language():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    [instruction] = judge.instructions(0)
    assert isinstance(instruction, OverlayInstruction) and instruction.serial >= 1
    assert isinstance(judge.verdict(0).outputs[0], OutputVerdict)

