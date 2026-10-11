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

import threading
from dataclasses import replace

from contracts.node_output import OutputReport, PictureReport, PowerAttempt

from .output_power import ReportSink


class OutputReports:
    """`ReportSink` for the power controller and `picture.PictureReports` for the picture
    controller, over the display line's sink. Thread-safe; never blocks."""

    def __init__(self, sink: ReportSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._power: dict[str, OutputReport] = {}           # the power controller's latest
        self._pictures: dict[str, PictureReport | None] = {}  # the picture controller's latest
        self._put: dict[str, OutputReport] = {}             # the merged report last put

    def put_report(self, report: OutputReport) -> None:
        """The power controller's report (its `picture` is ignored): merged and put if it differs."""
        with self._lock:
            self._power[report.output_id] = report
            self._merge(report.output_id)

    def emit_attempt(self, attempt: PowerAttempt) -> None:
        """Forwarded to the sink unchanged."""
        self._sink.emit_attempt(attempt)

    def put_picture(self, output_id: str, picture: PictureReport | None) -> None:
        """The picture controller's part: merged into the Output's latest report and put if it
        differs; held until the Output's first power report when none was put yet."""
        with self._lock:
            self._pictures[output_id] = picture
            self._merge(output_id)

    def _merge(self, output_id: str) -> None:
        """Under the lock: the Output's merged report, put only when it differs from the last.
        The sink never blocks (the bus session queues), so holding the lock keeps puts in order."""
        power = self._power.get(output_id)
        if power is None:
            return
        picture = self._pictures.get(output_id) if power.connected else None
        report = replace(power, picture=picture)
        if self._put.get(output_id) != report:
            self._put[output_id] = report
            self._sink.put_report(report)


__all__ = ["OutputReports"]
