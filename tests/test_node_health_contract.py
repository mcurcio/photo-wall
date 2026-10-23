"""The health judge's verdict on the node bus (contracts/node_health.py, issue #126): the wire shape,
its canonical codec and the checks every report and Output entry takes."""
from __future__ import annotations

import json

import pytest

from contracts.node_faults import catalogue_digest
from contracts.node_health import (
    HEALTH_REPORT_BYTES,
    MAX_HEALTH_OUTPUTS,
    HealthReport,
    OutputHealth,
    decode_health_report,
    encode_health_report,
)

SINCE = "2026-10-20T08:10:00Z"
DARK = OutputHealth("HDMI-A-1", True, "app_absent", SINCE)
LIT = OutputHealth("Virtual-1", False, None, None)


def _report(*outputs: OutputHealth, sequence: int = 7) -> HealthReport:
    return HealthReport(sequence, catalogue_digest(), outputs)


def test_a_report_round_trips_through_its_canonical_bytes():
    report = _report(DARK, LIT)
    raw = encode_health_report(report)
    assert decode_health_report(raw) == report
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    assert decode_health_report(encode_health_report(_report())).outputs == ()


@pytest.mark.parametrize("fields", [
    ("HDMI-A-1", True, None, SINCE),                # dark with no code
    ("HDMI-A-1", True, "app_absent", None),          # dark with no time
    ("HDMI-A-1", True, "App-Absent", SINCE),         # not a catalogue-shaped code
    ("HDMI-A-1", True, "app_absent", "08:10"),       # not a set time
    ("HDMI-A-1", True, "app_absent", "2026-02-30T08:10:00Z"),  # not a calendar moment
    ("HDMI-A-1", False, "app_absent", None),         # lit with a code
    ("HDMI-A-1", False, None, SINCE),                # lit with a time
    ("HDMI-A-1", 1, None, None),                     # dark is not a bool
    ("-HDMI", False, None, None),                    # not a node token
    ("H" * 97, False, None, None),                   # longer than 96
])
def test_an_output_entry_is_refused_outside_its_rule(fields):
    with pytest.raises(ValueError, match="^output_health$"):
        OutputHealth(*fields)


@pytest.mark.parametrize("build", [
    lambda: HealthReport(-1, catalogue_digest(), ()),
    lambda: HealthReport(True, catalogue_digest(), ()),
    lambda: HealthReport(0, "ABC", ()),
    lambda: HealthReport(0, catalogue_digest(), [DARK]),
    lambda: HealthReport(0, catalogue_digest(), (LIT, DARK)),     # not in output_id order
    lambda: HealthReport(0, catalogue_digest(), (DARK, DARK)),    # an Output twice
    lambda: HealthReport(0, catalogue_digest(), tuple(
        OutputHealth(f"HDMI-A-{index:02}", False, None, None) for index in range(MAX_HEALTH_OUTPUTS + 1))),
])
def test_a_report_is_refused_outside_its_rule(build):
    with pytest.raises(ValueError, match="^health_report$"):
        build()


def test_the_largest_report_fits_its_key():
    widest = tuple(OutputHealth(f"{index:02}" + "O" * 94, True, "a" * 64, SINCE)
                   for index in range(MAX_HEALTH_OUTPUTS))
    assert len(encode_health_report(_report(*widest, sequence=2**63 - 1))) <= HEALTH_REPORT_BYTES


@pytest.mark.parametrize("raw", [
    b"not json",
    b"{}",
    json.dumps({"sequence": 1, "catalogue": catalogue_digest(), "outputs": [], "extra": 1}).encode(),
    json.dumps({"sequence": 1, "catalogue": catalogue_digest(), "outputs": [{"output_id": "HDMI-A-1"}]}).encode(),
    json.dumps({"sequence": 1, "catalogue": catalogue_digest(), "outputs": {}}).encode(),
    b'{"sequence": 1, "sequence": 2, "catalogue": "", "outputs": []}',
    b" " * (HEALTH_REPORT_BYTES + 1),
])
def test_anything_but_a_report_decodes_to_the_one_refusal(raw):
    with pytest.raises(ValueError, match="^health_report$"):
        decode_health_report(raw)


def test_encoding_takes_only_a_report():
    with pytest.raises(ValueError, match="^health_report$"):
        encode_health_report({"sequence": 1})
