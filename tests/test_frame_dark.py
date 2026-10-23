"""A dark Frame as Central words it from its Pi's health verdict (central/fleet/frame_dark.py, issue
#126): only the bound Output's entry counts, worded by the catalogue's household line."""
from __future__ import annotations

from central.fleet.frame_dark import FrameDark, frame_dark
from contracts.node_faults import FAULTS, catalogue_digest
from contracts.node_health import HealthReport, OutputHealth

SINCE = "2026-10-20T08:10:00Z"


def _report(*outputs: OutputHealth) -> HealthReport:
    return HealthReport(5, catalogue_digest(), outputs)


def test_the_bound_outputs_dark_entry_is_the_frames_with_its_household_line():
    report = _report(OutputHealth("HDMI-A-1", True, "app_descriptor_limit", SINCE),
                     OutputHealth("HDMI-A-2", False, None, None))
    assert frame_dark(report, "HDMI-A-1") == FrameDark(
        code="app_descriptor_limit", line=FAULTS["app_descriptor_limit"].household_line, since=SINCE)
    assert frame_dark(report, "HDMI-A-2") is None          # lit
    assert frame_dark(report, "HDMI-A-3") is None          # not in the verdict
    assert frame_dark(report, None) is None                # unbound
    assert frame_dark(None, "HDMI-A-1") is None            # no verdict


def test_only_the_bound_outputs_entry_counts_even_when_another_is_dark():
    report = _report(OutputHealth("HDMI-A-1", True, "app_absent", SINCE),
                     OutputHealth("HDMI-A-2", False, None, None))
    assert frame_dark(report, "HDMI-A-2") is None


def test_a_code_this_centrals_catalogue_lacks_is_shown_as_the_code():
    report = _report(OutputHealth("HDMI-A-1", True, "a_newer_fault", SINCE))
    assert frame_dark(report, "HDMI-A-1") == FrameDark(code="a_newer_fault", line="a_newer_fault", since=SINCE)
