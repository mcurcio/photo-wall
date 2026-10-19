"""The Outputs the Player enrolls carry their modes as built (render planes S3, bead B6; S2's
erratum: a Pi enrolled 0 x 0, so Central sized no photo by the rectangle)."""
from __future__ import annotations

from contracts.enrollment import OutputReport
from player.output_discovery import measured


def _output(output_id: str, connected: bool = True) -> OutputReport:
    return OutputReport(output_id=output_id, width_px=0, height_px=0, connected=connected)


def test_a_connected_output_takes_its_mode_and_the_order_is_kept():
    outputs = (_output("HDMI-A-2"), _output("HDMI-A-1"))
    assert measured(outputs, {"HDMI-A-1": (1920, 1080), "HDMI-A-2": (3840, 2160)}) == (
        OutputReport(output_id="HDMI-A-2", width_px=3840, height_px=2160, connected=True),
        OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080, connected=True))


def test_an_output_without_a_mode_or_disconnected_keeps_zero_by_zero():
    outputs = (_output("HDMI-A-1"), _output("HDMI-A-2", connected=False))
    assert measured(outputs, {"HDMI-A-2": (1920, 1080)}) == outputs
    assert measured((), {"HDMI-A-1": (1920, 1080)}) == ()
