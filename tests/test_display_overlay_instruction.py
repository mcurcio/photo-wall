"""Display's published overlay language: bounded instructions and presented reports."""

from __future__ import annotations

import pytest

from appliance.display_host.overlay.instruction import (
    INSTRUCTION_STALE_MS,
    PULSE_DEADLINE_MS,
    UNAVAILABLE_LINES,
    OverlayInstruction,
    PresentedReport,
    encode_overlay_instruction,
    encode_presented_report,
    parse_overlay_instruction,
    parse_presented_report,
)

LINES = ("Photos paused — the player stopped responding", "app_unresponsive · Player p · Output o")


def test_constants():
    assert (PULSE_DEADLINE_MS, INSTRUCTION_STALE_MS) == (3000, 15000)
    assert UNAVAILABLE_LINES == ("Health status unavailable", "")
    assert OverlayInstruction("Virtual-1", 0, True, UNAVAILABLE_LINES).lines == UNAVAILABLE_LINES


def test_instruction_round_trips():
    instruction = OverlayInstruction("Virtual-1", 7, True, LINES)
    assert parse_overlay_instruction(encode_overlay_instruction(instruction)) == instruction
    off = OverlayInstruction("HDMI-A-1", 0, False, ("", ""))
    assert parse_overlay_instruction(encode_overlay_instruction(off)) == off


def test_report_round_trips():
    report = PresentedReport("Virtual-1", 12)
    assert parse_presented_report(encode_presented_report(report)) == report


@pytest.mark.parametrize("arguments", [
    ("", 1, True, ("", "")),                        # no Output
    ("o" * 97, 1, True, ("", "")),                  # Output over 96
    ("o", -1, True, ("", "")),                      # serial below 0
    ("o", True, True, ("", "")),                    # bool is not a serial
    ("o", 1, 1, ("", "")),                          # tint is a bool
    ("o", 1, True, ("x" * 97, "")),                 # line over 96
    ("o", 1, True, ("", "x" * 97)),
    ("o", 1, True, ("only one",)),
    ("o", 1, True, ["a", "b"]),                     # lines is a tuple
    ("o", 1, True, ("a", 2)),
])
def test_instruction_bounds_hold_at_construction(arguments):
    with pytest.raises(ValueError, match="overlay_instruction"):
        OverlayInstruction(*arguments)


def test_instruction_boundaries_admitted():
    OverlayInstruction("o" * 96, 0, False, ("x" * 96, "y" * 96))


@pytest.mark.parametrize("arguments", [("", 1), ("o" * 97, 1), ("o", -1), ("o", 1.0), ("o", False)])
def test_report_bounds_hold_at_construction(arguments):
    with pytest.raises(ValueError, match="presented_report"):
        PresentedReport(*arguments)


@pytest.mark.parametrize("raw", [
    b"",
    b"not json",
    b"\xff",
    b"[]",
    b'{"output":"o","serial":1,"tint":true}',
    b'{"output":"o","serial":1,"tint":true,"lines":["",""],"extra":1}',
    b'{"output":"o","serial":1,"tint":true,"lines":["",""],"serial":2}',
    b'{"output":"o","serial":1,"tint":true,"lines":"ab"}',
    b'{"output":"o","serial":1,"tint":true,"lines":["","",""]}',
    b'{"output":"o","serial":1.0,"tint":true,"lines":["",""]}',
    b'{"output":"o","serial":NaN,"tint":true,"lines":["",""]}',
    b'{"output":"o","serial":1,"tint":"yes","lines":["",""]}',
])
def test_instruction_parse_refuses(raw):
    with pytest.raises(ValueError, match="overlay_instruction"):
        parse_overlay_instruction(raw)


@pytest.mark.parametrize("raw", [
    b'{"output":"o"}',
    b'{"output":"o","serial":1,"tint":true}',
    b'{"output":"o","serial":"1"}',
    b'{"output":"o","serial":1,"output":"p"}',
    b"[1]",
])
def test_report_parse_refuses(raw):
    with pytest.raises(ValueError, match="presented_report"):
        parse_presented_report(raw)
