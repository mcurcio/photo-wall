"""The health judge's verdict onto its Node's bus (issue #126): the health line's slice, the report
the judge builds from its verdict (appliance/health/report.py), the sink that only queues it, and the
runner that puts each new verdict once. Central receiving it through a real bus and hub is the
node-pid1 `dark` leg's (tests/test_node_pid1.py)."""
from __future__ import annotations

import time

from appliance.health import runner
from appliance.health.bus import HEALTH_SLICE, HEALTH_STATE, BusHealthSink, health_session
from appliance.health.judge import OutputVerdict, Verdict
from appliance.health.report import health_report
from contracts.node_faults import catalogue_digest
from contracts.node_health import (
    HEALTH_COMPONENT,
    HEALTH_REPORT_BYTES,
    HEALTH_REPORT_KEY,
    HealthReport,
    OutputHealth,
    decode_health_report,
)
from contracts.node_link import STORE_LINES
from contracts.node_output import encode_set_time
from nodeapi.buffers import Role, role_of

WALL_MS = 1_792_000_000_123     # the Pi's wall clock while the boot clock reads NOW_MS
NOW_MS = 600_000


def _verdict(sequence: int, *outputs: OutputVerdict) -> Verdict:
    return Verdict(sequence, (), outputs)


def test_the_health_line_holds_the_verdict_key_and_records_take_the_rest():
    line = STORE_LINES[HEALTH_COMPONENT]
    assert HEALTH_STATE.sizes[HEALTH_REPORT_KEY] == HEALTH_REPORT_BYTES
    roles = sorted(role_of(config).value for config in HEALTH_SLICE.buffers)
    assert roles == sorted([Role.EVENTS.value, Role.STATE.value])
    assert sum(config.max_bytes for config in HEALTH_SLICE.buffers) == line.max_bytes
    assert len(HEALTH_SLICE.buffers) <= line.streams


def test_a_dark_output_carries_its_card_and_when_its_evidence_began_on_the_wall_clock():
    verdict = _verdict(4, OutputVerdict("HDMI-A-1", "slate", ("app_absent",), "app_absent", 65_000),
                       OutputVerdict("HDMI-A-2", "live", ("app_descriptor_pressure",), None, None))
    report = health_report(verdict, now_ms=NOW_MS, wall_ms=WALL_MS, catalogue=catalogue_digest())
    assert report == HealthReport(4, catalogue_digest(), (
        OutputHealth("HDMI-A-1", True, "app_absent", encode_set_time((WALL_MS - 65_000) // 1000)),
        OutputHealth("HDMI-A-2", False, None, None)))


def test_the_sink_holds_the_encoded_report_on_the_session_without_a_bus():
    session = health_session("digest")          # never started: no bus at all
    report = HealthReport(1, catalogue_digest(), (OutputHealth("Virtual-1", True, "app_absent",
                                                               "2026-10-20T08:10:00Z"),))
    began = time.monotonic()
    BusHealthSink(session).put(report)
    assert time.monotonic() - began < 0.5        # it only queues
    assert decode_health_report(session.state.get(HEALTH_REPORT_KEY)) == report


class _Judge:
    """A judge whose verdict is whatever the test sets."""

    def __init__(self) -> None:
        self.current = _verdict(0)

    def verdict(self, now_ms: int) -> Verdict:
        return self.current

    def instructions(self, now_ms, *, wall_ms, zone):
        return ()


class _Sink:
    def __init__(self) -> None:
        self.put_reports: list[HealthReport] = []

    def put(self, report: HealthReport) -> None:
        self.put_reports.append(report)


def test_the_runner_puts_each_new_verdict_once():
    judge, sink = _Judge(), _Sink()
    health = runner.HealthRunner(judge, (), clock=lambda: NOW_MS, wall=lambda: WALL_MS, sink=sink)
    health.turn()
    health.turn()
    assert [report.sequence for report in sink.put_reports] == [0]
    judge.current = _verdict(3, OutputVerdict("Virtual-1", "slate", ("app_absent",), "app_absent", 1_000))
    health.turn()
    health.turn()
    assert [report.sequence for report in sink.put_reports] == [0, 3]
    assert sink.put_reports[-1].outputs[0].dark is True


def test_a_verdict_outside_the_contract_is_skipped_and_the_judge_keeps_running():
    judge, sink = _Judge(), _Sink()
    judge.current = _verdict(1, OutputVerdict("not a token", "live", (), None, None))
    health = runner.HealthRunner(judge, (), clock=lambda: NOW_MS, wall=lambda: WALL_MS, sink=sink)
    health.turn()
    judge.current = _verdict(2, OutputVerdict("HDMI-A-1", "live", (), None, None))
    health.turn()
    assert [report.sequence for report in sink.put_reports] == [2]


def test_without_a_sink_the_runner_puts_nothing_and_still_turns():
    runner.HealthRunner(_Judge(), (), clock=lambda: NOW_MS).turn()
