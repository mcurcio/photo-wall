"""103 over a database as 102 left it: every Display gains its size, unknown (NULL) until a Pi on the
new release reports one, and every stored Output report decodes under the display line's schema
major 10 with `size_mm` null (frame box run, .claude/runs/frame-box/page.md, bead fb-2; no
backwards-compatible decoder). A Display's other columns and its Output's row are left as they were.

The rows are seeded through today's code on the whole schema, then 103 is undone on the real tables
(`before_103`: the two columns dropped, each stored report as 102 stored it, without `size_mm`), as
test_migration_101 does.
"""
from __future__ import annotations

import json

from support.migrations import migrate_before
from test_display_identity import MODE, NO_SERIAL, _bind, _frame, _report, _sql, enroll_pi

from contracts.node_output import OutputReport, decode_output_report, encode_output_report

SERIAL = "serial-migration-103"
DISPLAY_COLUMNS = "SELECT id, maker, product, name, serial, frame_id, modes FROM displays ORDER BY id"
OUTPUT_COLUMNS = "SELECT output_id, display_id, identity, connected FROM output_displays ORDER BY output_id"


def before_103(registry) -> None:
    """Undo 103 on the real tables: no displays.width_mm or height_mm, each stored Output report
    without its size, and 103 not in the ledger."""
    _sql(registry, "ALTER TABLE displays DROP COLUMN width_mm, DROP COLUMN height_mm")
    _sql(registry, "UPDATE output_displays SET report = report - 'size_mm'")
    _sql(registry, "DELETE FROM schema_migrations WHERE name = '103_display_size.sql'")


def test_103_gives_every_display_an_unknown_size_and_every_stored_report_schema_10(registry):
    """Mutation probe: drop 103's `UPDATE output_displays` -> the stored reports do not decode
    (ValueError output_report) and this test fails."""
    player = enroll_pi(registry, SERIAL)
    _bind(registry, _frame(registry, "kitchen"), player, "HDMI-A-1")
    for output_id, size_mm in (("HDMI-A-1", (950, 540)), ("HDMI-A-2", None)):
        report = OutputReport(output_id, True, NO_SERIAL, MODE, (), None, None, None, None, size_mm)
        _report(registry, SERIAL, output_id, NO_SERIAL, data=encode_output_report(report))
    displays, seen = _sql(registry, DISPLAY_COLUMNS), _sql(registry, OUTPUT_COLUMNS)
    assert len(displays) == 1 and len(seen) == 2
    before_103(registry)
    assert all("size_mm" not in row["report"] for row in _sql(registry, "SELECT report FROM output_displays"))

    migrate_before(registry.db, "104")

    assert _sql(registry, "SELECT width_mm, height_mm FROM displays") == [{"width_mm": None, "height_mm": None}]
    assert _sql(registry, DISPLAY_COLUMNS) == displays
    assert _sql(registry, OUTPUT_COLUMNS) == seen
    for row in _sql(registry, "SELECT report FROM output_displays"):
        report = decode_output_report(json.dumps(row["report"]).encode())
        assert report.size_mm is None and report.modes == MODE and report.identity == NO_SERIAL
