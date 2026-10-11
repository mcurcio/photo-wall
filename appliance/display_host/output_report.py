"""Each Output's one report, assembled from its two writers (roadmap 1c; run ledger
.claude/runs/display-1c.md, slice P1).

The power controller (output_power.OutputPowerController) writes an Output's report with no
picture part; the picture controller (picture.PictureController) writes only the picture part.
`OutputReports` keeps the latest of each and puts the merged report to the display line.

Rules this module owns (one home each): an Output's report is the power controller's latest with
the picture controller's latest picture part; it is put only when it differs from the Output's
last put (an unchanged Output costs nothing on the bus); `connected` False puts picture None.
"""
from __future__ import annotations

from contracts.node_output import OutputReport, PictureReport, PowerAttempt

from .output_power import ReportSink


class OutputReports:
    """`ReportSink` for the power controller and `picture.PictureReports` for the picture
    controller, over the display line's sink. Thread-safe; never blocks."""

    def __init__(self, sink: ReportSink) -> None:
        raise NotImplementedError

    def put_report(self, report: OutputReport) -> None:
        """The power controller's report (its `picture` is ignored): merged and put if it differs."""
        raise NotImplementedError

    def emit_attempt(self, attempt: PowerAttempt) -> None:
        """Forwarded to the sink unchanged."""
        raise NotImplementedError

    def put_picture(self, output_id: str, picture: PictureReport | None) -> None:
        """The picture controller's part: merged into the Output's latest report and put if it
        differs; held until the Output's first power report when none was put yet."""
        raise NotImplementedError


__all__ = ["OutputReports"]
