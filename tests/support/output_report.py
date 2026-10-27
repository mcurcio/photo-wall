"""An Output report for Players with no Node (tests, scripts/demo_wall.py).

A Frame's screen size comes from its Output's report on the Node's display line (`current_mode`,
then the seen Display's preferred mode; `central.displays.model.screen_px`, issue #124), never from
the Player's enrolment. A Player run without a Node sends no such report, so `report_modes`
records the one its display line would have sent, through Central's real OutputReportJudge in one
transaction, as the hub's link store commits it.
"""
from __future__ import annotations

import itertools

from central.infra.display_store import REPORT_STREAM, DisplayWakes, OutputReportJudge
from contracts.node_output import OutputReport, encode_output_report, output_report_key
from nodeapi.epoch import Token
from nodeapi.pull import Read

_REPORT_SEQ = itertools.count(1)


def report_modes(registry, device_id: str, output_ids: tuple[str, ...], mode: tuple[int, int] | None) -> None:
    """Each Output's report on `device_id`'s display line, stating the mode Weston runs
    (`current_mode`) with no Display seen, or disconnected with none when `mode` is None. A database
    migrated only part way (the upgrade history tests) has no display line yet: nothing is recorded
    there."""
    reads = tuple(
        Read(Token("0123456789abcdef", next(_REPORT_SEQ)), "$KV.state_display." + output_report_key(output_id), {},
             encode_output_report(OutputReport(output_id, mode is not None, None, (), (), None, None, None, None, None,
                                               current_mode=mode)))
        for output_id in output_ids)
    with registry.db.transaction() as conn:
        if conn.execute("SELECT to_regclass('output_displays') AS line").fetchone()["line"] is not None:
            OutputReportJudge(DisplayWakes()).judge_in(conn, device_id, REPORT_STREAM, reads)
