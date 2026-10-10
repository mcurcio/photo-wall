"""Guard (1b D2, E-1B-5): an Output that drops out for a hot-plug blip, or for the whole of a
requested off (some displays drop hot-plug through standby), raises no display-affecting code.
Green before and after D2: the judge reads no display power, and must not start to."""
from uuid import uuid4

from appliance.apps.probe import SHIPPED_TIMING, AppRunKey
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.feed import FeedEvent
from appliance.health.judge import HealthJudge
from appliance.kernel.app_facts import APP_STARTED
from contracts.node_faults import FAULTS

RUN = AppRunKey(uuid4(), 101, 7, 1).document()
TEST_OFF_MS = 300_000           # central.displays TEST_SECONDS: the console's Test off


def judge() -> HealthJudge:
    return HealthJudge(period_ms=SHIPPED_TIMING.period_ms, miss_limit=SHIPPED_TIMING.miss_limit,
                       startup_ms=SHIPPED_TIMING.startup_ms,
                       kill_after_ms=SHIPPED_TIMING.kill_after_ms,
                       pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)


def outputs(subject: HealthJudge, now: int, *, connected: bool) -> None:
    subject.observe_outputs([{"output_id": "HDMI-A-1", "connected": connected, "admitted": None}], now)


def display_affecting(subject: HealthJudge, now: int) -> list[str]:
    return [condition.code for condition in subject.verdict(now).conditions
            if FAULTS[condition.code].display_affecting]


def started() -> HealthJudge:
    subject = judge()
    outputs(subject, 0, connected=True)
    subject.observe(FeedEvent(0, APP_STARTED, {"run": RUN}, "node"), 0)
    return subject


def test_a_hot_plug_blip_raises_nothing_display_affecting():
    subject = started()
    outputs(subject, 1_000, connected=False)
    assert display_affecting(subject, 1_100) == []
    outputs(subject, 1_250, connected=True)
    assert display_affecting(subject, 10_000) == []
    assert [instruction.tint for instruction in subject.instructions(10_000)] == [False]


def test_an_output_gone_for_a_requested_off_raises_nothing_display_affecting():
    subject = started()
    outputs(subject, 1_000, connected=False)
    for now in range(1_000, 1_000 + TEST_OFF_MS + 1, 30_000):
        assert display_affecting(subject, now) == []
    outputs(subject, 1_000 + TEST_OFF_MS, connected=True)
    assert display_affecting(subject, 2_000 + TEST_OFF_MS) == []
