"""094 (display tuning): the Display's signal range and the picture session's test pattern.

The migration's SQL runs on the real tables after undoing it (`before_094`), with rows as the print
look's build stored them: every existing Display reads Auto, so its signal is unchanged; open
picture sessions are dropped (their drafts lack the signal range); a range outside Auto, Full and
Limited, a pattern outside the three, and a pattern without its ask (or an ask without a pattern)
are refused.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from test_display_identity import NO_SERIAL, _bind, _frame, _report, _sql, enroll_pi

from central.infra.picture_columns import stored_picture
from contracts.node_picture import SignalRange

MIGRATION = Path(__file__).parents[1] / "central/migrations/094_display_tuning.sql"
SERIAL = "pi-094"


def before_094(registry) -> None:
    """The database as the print look's build left it: no signal range, no session test pattern."""
    _sql(registry, "ALTER TABLE picture_sessions DROP CONSTRAINT picture_sessions_test_pattern_ask, "
                   "DROP COLUMN test_pattern, DROP COLUMN test_pattern_ask")
    _sql(registry, "ALTER TABLE displays DROP COLUMN signal_range")


def migrate_094(registry, migration: str | None = None) -> None:
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION.read_text() if migration is None else migration)


def _display_id(registry) -> str:
    player = enroll_pi(registry, SERIAL)
    _bind(registry, _frame(registry, "kitchen"), player, "HDMI-A-1")
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    [row] = _sql(registry, "SELECT display_id FROM output_displays WHERE player_id=%s", (player,))
    return row["display_id"]


def _open_session(registry, display_id: str, columns: str = "", values: str = "") -> None:
    _sql(registry, f"INSERT INTO picture_sessions(id, display_id, frame_id, sequence, draft, began_at_change, "  # noqa: S608
                   f"idle_until, created_at{columns}) VALUES (gen_random_uuid(), %s, 'kitchen', 1, '{{}}', 0, 9e9, 0"
                   f"{values})", (display_id,))


def test_094_existing_displays_read_auto_and_open_sessions_go(registry):
    """Mutation probe: default signal_range to 'full' in a scratch copy -> the default assertion fails."""
    display_id = _display_id(registry)
    before_094(registry)
    _open_session(registry, display_id)
    migrate_094(registry)

    [row] = _sql(registry, "SELECT * FROM displays WHERE id=%s", (display_id,))
    assert row["signal_range"] == "auto" and stored_picture(row).signal_range is SignalRange.AUTO
    assert _sql(registry, "SELECT id FROM picture_sessions") == []


def test_094_refuses_a_range_or_pattern_outside_the_vocabulary(registry):
    display_id = _display_id(registry)
    with pytest.raises(psycopg.errors.CheckViolation):
        _sql(registry, "UPDATE displays SET signal_range='studio' WHERE id=%s", (display_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        _open_session(registry, display_id, ", test_pattern, test_pattern_ask", ", 'bars', 2")
    with pytest.raises(psycopg.errors.CheckViolation):
        _open_session(registry, display_id, ", test_pattern, test_pattern_ask", ", 'white', 0")
    _open_session(registry, display_id, ", test_pattern, test_pattern_ask", ", 'black-level', 2")
    assert _sql(registry, "SELECT test_pattern, test_pattern_ask FROM picture_sessions") == [
        {"test_pattern": "black-level", "test_pattern_ask": 2}]


def test_094_a_pattern_needs_its_ask_and_an_ask_its_pattern(registry):
    """Mutation probe: migrate with a scratch copy of 094 whose picture_sessions_test_pattern_ask
    constraint is removed (`migrate_094(registry, <that text>)`) -> both inserts are taken and this
    fails."""
    display_id = _display_id(registry)
    before_094(registry)
    migrate_094(registry)
    for columns, values in ((", test_pattern", ", 'white'"), (", test_pattern_ask", ", 3")):
        with pytest.raises(psycopg.errors.CheckViolation):
            _open_session(registry, display_id, columns, values)
    assert _sql(registry, "SELECT id FROM picture_sessions") == []
