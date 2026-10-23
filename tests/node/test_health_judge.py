"""Health judge: the fault catalogue, construction, the app_unresponsive rules (a run cause pinned
until another run starts, issue #125), the card's time and the software_renderer rule."""
import datetime
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
    APP_ABSENT,
    APP_DESCRIPTOR_LIMIT,
    APP_RESOURCE_EXHAUSTED,
    APP_UNRESPONSIVE,
    CARD_PRECEDENCE,
    DESCRIPTOR_LIMIT_HEADROOM,
    MAX_OUTPUTS,
    NOTHING_TO_SHOW,
    RING_CAPACITY,
    SOFTWARE_RENDERER,
    HealthJudge,
    OutputVerdict,
    Presented,
    Transition,
    card_time,
    descriptor_at_limit,
    evidence_wall_s,
)
from contracts import node_faults
from contracts.node_faults import FAULTS, Fault, catalogue_digest

RAISE = FAULTS[APP_UNRESPONSIVE].raise_window_ms
# The Pi's wall clock read when the judge's boot clock read 0: 2026-10-20 07:17 UTC.
WALL_AT_0 = int(datetime.datetime(2026, 10, 20, 7, 17, tzinfo=datetime.UTC).timestamp() * 1000)
RUN = AppRunKey(uuid4(), 101, 7, 1).document()
NEXT_RUN = AppRunKey(uuid4(), 202, 9, 2).document()


def timing(**overrides):
    values = {"period_ms": SHIPPED_TIMING.period_ms, "miss_limit": SHIPPED_TIMING.miss_limit,
              "startup_ms": SHIPPED_TIMING.startup_ms,
              "pulse_deadline_ms": PULSE_DEADLINE_MS, "catalogue": FAULTS}
    return {**values, **overrides}


def fact(kind, run=RUN, **value):
    return FeedEvent(0, kind, {"run": run, **value}, "node")


def unanswered(run=RUN):
    return fact("probe_unanswered", run, unanswered_ms=12000, misses=6)


def answered(run=RUN):
    return fact("probe_answered", run, rtt_ms=3)


def started(run=NEXT_RUN):
    return fact("app_started", run)


def project(judge, now):
    """The judge's instructions at boot ms `now`, the wall clock WALL_AT_0 + now (UTC)."""
    return judge.instructions(now, wall_ms=WALL_AT_0 + now, zone=datetime.UTC)


def states(judge, now):
    return [(condition.code, condition.state) for condition in judge.verdict(now).conditions]


# -- catalogue -----------------------------------------------------------------------------


def test_the_catalogue_has_the_m1_row_the_degraded_rows_and_the_absent_app_row():
    assert dict(FAULTS) == {
        NOTHING_TO_SHOW: Fault(NOTHING_TO_SHOW, True, "Nothing to show on this Frame", 120000, 0),
        APP_DESCRIPTOR_LIMIT: Fault(
            APP_DESCRIPTOR_LIMIT, True, "Photos paused — the player ran out of open files", 0, 0),
        APP_UNRESPONSIVE: Fault(
            APP_UNRESPONSIVE, True, "Photos paused — the player stopped responding", 5000, 0),
        SOFTWARE_RENDERER: Fault(
            SOFTWARE_RENDERER, False, "Photos are drawn without the graphics processor", 0, 0),
        APP_ABSENT: Fault(APP_ABSENT, True, "Photos paused — the player stopped", 5000, 0),
        APP_RESOURCE_EXHAUSTED: Fault(
            APP_RESOURCE_EXHAUSTED, False, "The player is running out of open files", 0, 60000)}
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


# -- construction ----------------------------------------------------------------------------


def test_the_shipped_judge_builds_from_the_published_constants_with_no_kill_time():
    """Nothing kills (issue #125): the judge takes no K, and the shipped one builds."""
    judge = runner.shipped_judge()
    assert judge.verdict(0).conditions == ()
    assert not hasattr(SHIPPED_TIMING, "kill_after_ms")
    with pytest.raises(TypeError):
        HealthJudge(**timing(), kill_after_ms=35000)


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


def test_a_frozen_run_that_answers_again_keeps_its_card_until_a_new_run():
    """Issue #125: a raised app_unresponsive is pinned to its run. Its own answers (a frozen
    Player that thaws) never clear it, however long they hold; only another run starting does
    (reason `started`). Mutation probe: let probe_answered start the clear hold of a raised
    app_unresponsive -> fails."""
    judge = raised_judge()
    for now in range(RAISE + 2000, RAISE + 600_000, 2000):
        judge.observe(answered(), now)
    assert states(judge, RAISE + 600_000) == [(APP_UNRESPONSIVE, "raised")]
    judge.observe(started(RUN), RAISE + 600_001)  # the same run again is no new run
    assert states(judge, RAISE + 600_001) == [(APP_UNRESPONSIVE, "raised")]
    judge.observe(started(), RAISE + 600_002)
    assert states(judge, RAISE + 600_002) == []
    assert [(t.state, t.reason) for t in judge.transitions()][-1] == ("cleared", "started")


def test_a_new_run_starting_withdraws_a_pending_unresponsive_condition():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.observe(started(), 1)
    assert states(judge, RAISE) == []
    assert [(t.state, t.reason) for t in judge.transitions()][-1] == ("withdrawn", "started")


def test_a_new_run_does_not_inherit_an_old_runs_window():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.observe(unanswered(NEXT_RUN), RAISE - 1)
    verdict = judge.verdict(RAISE)
    assert [(c.state, c.run) for c in verdict.conditions] == [("pending", NEXT_RUN)]


def test_a_feed_gap_withdraws_pending_but_keeps_raised():
    judge = HealthJudge(**timing())
    judge.observe(unanswered(), 0)
    judge.forget(1000)
    assert states(judge, 1000 + RAISE) == []
    judge = raised_judge()
    judge.observe(answered(), RAISE + 1000)
    judge.forget(RAISE + 2000)
    assert states(judge, RAISE + 600_000) == [(APP_UNRESPONSIVE, "raised")]


@pytest.mark.parametrize("event", [
    fact("probe_channel", state="open"), fact("probe_channel", state="closed"),
    fact("app_link_recorded"), fact("relink_sent"),
    fact("app_link_refused", status=409, reason="central_refused"),
    fact("kill_withheld", reason="recovery_armed"),
    fact("app_killed", reason="other", unanswered_ms=1),
    # The removed kill's facts (issue #125) mean nothing to the judge.
    fact("app_killed", reason="unresponsive", unanswered_ms=35000),
    fact("probe_kill_due", unanswered_ms=35000),
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


# -- software_renderer: degraded, never a tint ---------------------------------------------

LLVMPIPE = "llvmpipe (LLVM 19.1.7, 128 bits)"  # the Pi's Player before the render-node grant
V3D = "V3D 7.1.10.2"


def renderer(name, run=RUN):
    return fact("app_renderer", run, renderer=name)


@pytest.mark.parametrize("name", [LLVMPIPE, "softpipe", "Software Rasterizer",
                                  "zink Vulkan 1.3(llvmpipe (LLVM 19.1.7, 128 bits))"])
def test_a_software_renderer_raises_the_degraded_condition_at_once(name):
    judge = HealthJudge(**timing())
    judge.observe(renderer(name), 0)
    assert states(judge, 0) == [(SOFTWARE_RENDERER, "raised")]
    assert [(t.code, t.state, t.reason) for t in judge.transitions()] == [
        (SOFTWARE_RENDERER, "raised", "software_renderer")]


def test_a_gpu_renderer_is_no_condition_and_clears_a_software_one():
    judge = HealthJudge(**timing())
    judge.observe(renderer(V3D), 0)
    assert judge.verdict(0).conditions == () and judge.transitions() == ()
    judge.observe(renderer(LLVMPIPE), 10)
    judge.observe(renderer(LLVMPIPE, NEXT_RUN), 20)  # a respawned run, still in software
    assert [(c.code, c.run, c.state) for c in judge.verdict(20).conditions] == [
        (SOFTWARE_RENDERER, NEXT_RUN, "raised")]
    judge.forget(30)  # a feed gap keeps it, like any raised condition
    judge.observe(renderer(V3D, NEXT_RUN), 40)
    assert judge.verdict(40).conditions == ()
    assert [(t.state, t.reason) for t in judge.transitions()] == [
        ("raised", "software_renderer"), ("raised", "run_changed"), ("cleared", "gpu_renderer")]


def test_software_rendering_is_shown_on_each_output_but_never_tints():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    judge.observe(renderer(LLVMPIPE), 0)
    assert underlays(judge, 0) == [("Virtual-1", "live", (SOFTWARE_RENDERER,))]
    assert cards(judge, 0) == [("Virtual-1", False, ("", ""))]


# -- per Output: the display snapshot, underlay and the overlay projection -----------------

LINE = FAULTS[APP_UNRESPONSIVE].household_line
OFF = ("", "")


def output(name="Virtual-1", run=RUN, *, connected=True, fault=None, holding=None, power="on"):
    """One entry of Display's `outputs` snapshot (appliance/display_host/runner.py); an admitted
    run holds the Output unless `holding` says otherwise."""
    admitted = None if run is None else {**run, "frame_id": "frame-1"}
    return {"output_id": name, "connected": connected, "admitted": admitted,
            "diagnostic": "released" if run else "presented", "fault": fault,
            "holding": run is not None if holding is None else holding, "power": power}


def underlays(judge, now):
    return [(o.output, o.underlay, o.codes) for o in judge.verdict(now).outputs]


def cards(judge, now):
    return [(i.output, i.tint, i.lines) for i in project(judge, now)]


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
        f"{LINE} · since 07:17", "app_unresponsive · Player player-1 · Output Virtual-1"))
    assert all(tint for _, tint, _ in cards(judge, RAISE))


def test_after_the_run_is_gone_the_slate_keeps_the_card_and_its_time():
    judge = raised_judge()
    judge.observe_outputs([output()], RAISE)
    judge.observe(fact("app_exited"), 30000)  # a person's kill -9 of the frozen run
    judge.observe_outputs([output(run=None, fault="surface_lease_or_process_lost")], 30001)
    assert underlays(judge, 30001) == [("Virtual-1", "slate", (APP_UNRESPONSIVE,))]
    assert cards(judge, 30001) == [("Virtual-1", True, (
        f"{LINE} · since 07:17", "app_unresponsive · Output Virtual-1"))]  # no link: no Player id
    absent = FAULTS[APP_ABSENT].household_line  # app_absent raised too: it comes first
    assert cards(judge, 30000 + RAISE) == [("Virtual-1", True, (
        f"{absent} · since 07:17", "app_absent · Output Virtual-1"))]


def test_the_serial_bumps_on_every_change_of_an_outputs_card_and_only_then():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(), output("HDMI-A-2", None)], 0)
    healthy = project(judge, 0)
    assert project(judge, 1) == healthy  # unchanged: same serials
    assert len({i.serial for i in healthy}) == 2
    judge.observe(unanswered(), 1)
    judge.observe(unanswered(), 1 + RAISE)
    tinted = project(judge, 1 + RAISE)
    assert len(tinted) == 2
    assert all(new.tint and new.serial > max(i.serial for i in healthy) for new in tinted)
    assert project(judge, 1 + RAISE + 600_000) == tinted  # the card's time does not move
    judge.observe(started(), RAISE + 2001)
    cleared = project(judge, RAISE + 2001)
    # HDMI-A-2, on with nothing admitted for ten minutes, still has nothing to show.
    assert [(i.tint, i.lines[1]) for i in cleared] == [
        (True, "nothing_to_show · Output HDMI-A-2"), (False, "")]
    assert all(new.serial > max(i.serial for i in tinted) for new in cleared)
    judge.observe(fact("app_link_accepted", player_id="player-2"), RAISE + 2001)
    assert project(judge, RAISE + 2002)[1] == cleared[1]  # lines unchanged while off


def test_a_new_player_id_changes_the_card_and_its_serial():
    judge = raised_judge()
    judge.observe_outputs([output()], RAISE)
    before = project(judge, RAISE)[0]
    judge.observe(fact("app_link_accepted", player_id="player-9"), RAISE)
    after = project(judge, RAISE)[0]
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
    judge.observe_outputs([output(run=None)], 2)  # the slate: nothing to show opens pending
    assert judge.verdict(2).sequence == 3 and judge.transitions() == (
        Transition(2, NOTHING_TO_SHOW, {"output": "Virtual-1"}, "pending", "not_held", 2),)


HELD = {"holding": False, "power": "on"}


@pytest.mark.parametrize("snapshot", [
    None, {}, "x", [1], [{"output_id": "", "connected": True, "admitted": None, **HELD}],
    [{"output_id": "V", "admitted": None, **HELD}],
    [{"output_id": "V", "connected": 1, "admitted": None, **HELD}],
    [{"output_id": "V", "connected": True, "admitted": {"pid": 1}, **HELD}],
    [{"output_id": "V", "connected": True, "admitted": {**RUN, "pid": "101"}, **HELD}],
    [{"output_id": "V", "connected": True, "admitted": None, **HELD}] * 2,
    [{"output_id": f"V{index}", "connected": True, "admitted": None, **HELD}
     for index in range(MAX_OUTPUTS + 1)],
    [{"output_id": "V", "connected": True, "admitted": None, "power": "on"}],     # no holding
    [{"output_id": "V", "connected": True, "admitted": None, "holding": False}],  # no power
    [{"output_id": "V", "connected": True, "admitted": None, "holding": 0, "power": "on"}],
    [{"output_id": "V", "connected": True, "admitted": None, "holding": False, "power": "dim"}],
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
    [instruction] = project(judge, RAISE)
    assert instruction.output == "Virtual-1" and len(instruction.lines[1]) == MAX_TEXT


def test_a_presented_serial_is_kept_in_the_ring_once():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    [instruction] = project(judge, 0)
    assert judge.presented(PresentedReport("Virtual-1", instruction.serial), 5) is True
    assert judge.presented(PresentedReport("Virtual-1", instruction.serial), 6) is False
    assert judge.presented(PresentedReport("Virtual-1", instruction.serial + 1), 6) is False
    assert judge.presented(PresentedReport("HDMI-A-2", instruction.serial), 6) is False
    assert judge.transitions() == (Presented(judge.sequence, "Virtual-1", instruction.serial, 5),)


def test_the_projection_is_displays_instruction_language():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    [instruction] = project(judge, 0)
    assert isinstance(instruction, OverlayInstruction) and instruction.serial >= 1
    assert isinstance(judge.verdict(0).outputs[0], OutputVerdict)



# -- the card's time: the Pi's own clock -----------------------------------------------------


def test_the_evidence_time_is_the_wall_clock_less_the_boot_clocks_own_interval():
    assert evidence_wall_s(since_ms=1_000, now_ms=61_000, wall_ms=1_700_000_060_500) == 1_700_000_000
    assert evidence_wall_s(since_ms=5, now_ms=5, wall_ms=999) == 0


def test_the_card_time_is_hours_and_minutes_in_the_pis_zone():
    at = int(datetime.datetime(2026, 10, 20, 7, 17, 59, tzinfo=datetime.UTC).timestamp())
    assert card_time(at, datetime.UTC) == "07:17"
    assert card_time(at, datetime.timezone(datetime.timedelta(hours=-7))) == "00:17"


def test_the_card_says_when_the_evidence_began_not_when_it_was_raised():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    judge.observe(unanswered(), 120_000)  # 07:19 on the wall clock
    [instruction] = project(judge, 120_000 + RAISE + 3_600_000)  # an hour later
    assert instruction.lines[0] == f"{LINE} · since 07:19"
    [moved] = judge.instructions(120_000 + RAISE + 3_600_000,
                                 wall_ms=WALL_AT_0 + 120_000 + RAISE + 3_600_000,
                                 zone=datetime.timezone(datetime.timedelta(hours=2)))
    assert moved.lines[0] == f"{LINE} · since 09:19" and moved.serial > instruction.serial


# -- issue #123: a Frame showing nothing, and the open-file limit ----------------------------

WINDOW = FAULTS[NOTHING_TO_SHOW].raise_window_ms
NOTHING = FAULTS[NOTHING_TO_SHOW].household_line
LIMIT_LINE = FAULTS[APP_DESCRIPTOR_LIMIT].household_line


def descriptors(open_descriptors, soft_limit=1024, run=RUN):
    return fact("app_descriptors", run, open=open_descriptors, soft_limit=soft_limit)


def test_the_card_precedence_puts_the_run_causes_before_the_frame_showing_nothing():
    assert CARD_PRECEDENCE == (APP_ABSENT, APP_UNRESPONSIVE, APP_DESCRIPTOR_LIMIT, NOTHING_TO_SHOW)
    assert all(FAULTS[code].display_affecting for code in CARD_PRECEDENCE)


@pytest.mark.parametrize(("open_descriptors", "soft_limit", "at_limit"), [
    (1024 - DESCRIPTOR_LIMIT_HEADROOM - 1, 1024, False), (1024 - DESCRIPTOR_LIMIT_HEADROOM, 1024, True),
    (1024, 1024, True), (0, 8, True), (900, 1024, False)])
def test_at_the_limit_is_within_the_headroom_of_the_soft_limit(open_descriptors, soft_limit, at_limit):
    """Mutation probe: '>' for '>=' -> the case exactly HEADROOM short is not at the limit."""
    assert descriptor_at_limit(open_descriptors, soft_limit) is at_limit


@pytest.mark.parametrize(("open_descriptors", "soft_limit"), [(-1, 10), (1, 0), ("1", 10), (1, True)])
def test_a_malformed_descriptor_fact_is_refused(open_descriptors, soft_limit):
    with pytest.raises(ValueError, match="^descriptor_fact$"):
        descriptor_at_limit(open_descriptors, soft_limit)


def test_nothing_to_show_raises_only_after_the_outcome_window():
    """An Output on with nothing admitted: pending (untinted) until the window passes, then the
    tint with the time its evidence began. Mutation probe: raise at once -> tinted at WINDOW - 1."""
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(run=None)], 60_000)  # 07:18
    assert cards(judge, 60_000 + WINDOW - 1) == [("Virtual-1", False, OFF)]
    assert underlays(judge, 60_000 + WINDOW - 1) == [("Virtual-1", "slate", ())]
    assert cards(judge, 60_000 + WINDOW) == [("Virtual-1", True, (
        f"{NOTHING} · since 07:18", "nothing_to_show · Output Virtual-1"))]
    [verdict] = judge.verdict(60_000 + WINDOW + 5).outputs
    assert (verdict.codes, verdict.card, verdict.card_age_ms) == (
        (NOTHING_TO_SHOW,), NOTHING_TO_SHOW, WINDOW + 5)


def test_a_run_that_stops_holding_its_frame_tints_after_the_window_and_clears_when_held_again():
    """The fireplace (2026-10-20): admitted, answering, showing nothing (the shell's `holding`
    false): tinted after the window; a picture plane again clears it at once."""
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(holding=False)], 0)
    judge.observe_outputs([output(holding=False)], WINDOW)
    assert cards(judge, WINDOW)[0][1] is True
    judge.observe_outputs([output(holding=True)], WINDOW + 1)
    assert cards(judge, WINDOW + 1) == [("Virtual-1", False, OFF)]
    assert [(t.code, t.state, t.reason) for t in judge.transitions()] == [
        (NOTHING_TO_SHOW, "pending", "not_held"), (NOTHING_TO_SHOW, "raised", "window_elapsed"),
        (NOTHING_TO_SHOW, "cleared", "held")]


def test_held_again_inside_the_window_withdraws_it_and_the_window_starts_over():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(holding=False)], 0)
    judge.observe_outputs([output()], WINDOW - 1)
    judge.observe_outputs([output(holding=False)], WINDOW)
    assert cards(judge, 2 * WINDOW - 1) == [("Virtual-1", False, OFF)]
    assert cards(judge, 2 * WINDOW)[0][1] is True


@pytest.mark.parametrize("case", [
    pytest.param(output(power="off"), id="night-off"),
    pytest.param(output(run=None, power="off"), id="resting-nothing-admitted"),
    pytest.param(output(holding=True), id="authored-black-a-black-layer-plane"),
    pytest.param(output(holding=True), id="pattern-display-drawing"),
])
def test_dark_on_purpose_and_patterns_give_no_tint(case):
    """Night off and Resting are the power in force off (the display off, or Display's black);
    authored black is the Player's black layer, a picture plane; a pattern or calibration target
    is Display's drawing: none is a Frame showing nothing, however long it lasts."""
    judge = HealthJudge(**timing())
    judge.observe_outputs([case], 0)
    judge.observe_outputs([case], 10 * WINDOW)
    assert cards(judge, 10 * WINDOW) == [("Virtual-1", False, OFF)]
    assert [t.code for t in judge.transitions()] == []


def test_powered_off_or_disconnected_withdraws_or_clears_it():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(holding=False), output("HDMI-A-2", None)], 0)
    judge.observe_outputs([output(holding=False, power="off"), output("HDMI-A-2", None)], WINDOW - 1)
    judge.observe_outputs([output(holding=False, power="off"),
                           output("HDMI-A-2", None, connected=False)], WINDOW + 1)
    assert cards(judge, WINDOW + 1) == [("Virtual-1", False, OFF)]
    assert [(t.run, t.state, t.reason) for t in judge.transitions()] == [
        ({"output": "Virtual-1"}, "pending", "not_held"), ({"output": "HDMI-A-2"}, "pending", "not_held"),
        ({"output": "Virtual-1"}, "withdrawn", "powered_off"),
        ({"output": "HDMI-A-2"}, "raised", "window_elapsed"),
        ({"output": "HDMI-A-2"}, "cleared", "disconnected")]


def test_at_the_limit_tints_at_once_and_pressure_does_not():
    """Pressure (80 %) is degraded: shown in the codes, never the tint; at the limit it tints at
    once. Mutation probe: app_descriptor_limit not display-affecting -> no tint at the limit."""
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    judge.observe(descriptors(900), 60_000)
    assert underlays(judge, 60_000) == [("Virtual-1", "live", (APP_RESOURCE_EXHAUSTED,))]
    assert cards(judge, 60_000) == [("Virtual-1", False, OFF)]
    judge.observe(descriptors(1024 - DESCRIPTOR_LIMIT_HEADROOM), 120_000)  # 07:19
    assert underlays(judge, 120_000) == [
        ("Virtual-1", "live", (APP_DESCRIPTOR_LIMIT, APP_RESOURCE_EXHAUSTED))]
    assert cards(judge, 120_000) == [("Virtual-1", True, (
        f"{LIMIT_LINE} · since 07:19", "app_descriptor_limit · Output Virtual-1"))]


def test_the_limit_is_pinned_to_its_run_until_another_run_starts():
    judge = HealthJudge(**timing())
    judge.observe_outputs([output()], 0)
    judge.observe(descriptors(1020), 0)
    judge.observe(descriptors(10), 70_000)       # relief: pressure clears after its hold, the limit stays
    judge.observe(descriptors(10), 140_000)
    assert underlays(judge, 140_000) == [("Virtual-1", "live", (APP_DESCRIPTOR_LIMIT,))]
    judge.observe(fact("app_exited"), 150_000)
    assert APP_DESCRIPTOR_LIMIT in judge.verdict(150_000 + 5000).outputs[0].codes
    judge.observe(started(), 160_000)
    assert APP_DESCRIPTOR_LIMIT not in judge.verdict(160_000).outputs[0].codes
    assert [(t.code, t.state, t.reason) for t in judge.transitions()
            if t.code == APP_DESCRIPTOR_LIMIT] == [
        (APP_DESCRIPTOR_LIMIT, "raised", "descriptor_limit"), (APP_DESCRIPTOR_LIMIT, "cleared", "started")]


def test_a_run_cause_comes_before_the_limit_and_the_frame_showing_nothing():
    """A frozen run at its limit whose Frame shows nothing: the card is app_unresponsive (first by
    CARD_PRECEDENCE), its time when that evidence began."""
    judge = HealthJudge(**timing())
    judge.observe_outputs([output(holding=False)], 0)
    judge.observe(descriptors(1020), 1000)
    judge.observe(unanswered(), 60_000)
    judge.observe_outputs([output(holding=False)], WINDOW)
    [verdict] = judge.verdict(WINDOW).outputs
    assert verdict.codes == (APP_DESCRIPTOR_LIMIT, APP_RESOURCE_EXHAUSTED, APP_UNRESPONSIVE,
                             NOTHING_TO_SHOW)
    assert (verdict.card, verdict.card_age_ms) == (APP_UNRESPONSIVE, WINDOW - 60_000)
    assert cards(judge, WINDOW)[0][2][0] == f"{LINE} · since 07:18"
