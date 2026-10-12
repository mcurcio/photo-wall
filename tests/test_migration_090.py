"""090 (Power and the week, #92, #94): every existing Frame is named "Frame 1", "Frame 2", ... in id
order, and each surface the Frames are on becomes a wall, "Wall 1", ... with no room label.

The migration's SQL runs on the real tables after undoing it (the name column, its index and the
walls table), with Frames stored as the previous build stored them.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from psycopg.errors import UniqueViolation
from test_display_identity import _frame

MIGRATION = Path(__file__).parents[1] / "central/migrations/090_names_and_rooms.sql"


def test_090_names_existing_frames_by_id_order_and_their_wall(registry):
    for frame in ("study", "hall", "attic"):
        _frame(registry, frame)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE frames SET surface_id='landing' WHERE id='attic'")
        conn.execute("DROP INDEX frames_name; ALTER TABLE frames DROP COLUMN name; DROP TABLE walls")
        conn.execute(MIGRATION.read_text())
        frames = conn.execute("SELECT id, name FROM frames ORDER BY id").fetchall()
        walls = conn.execute("SELECT surface_id, name, room FROM walls ORDER BY surface_id").fetchall()
    assert [(row["id"], row["name"]) for row in frames] == [("attic", "Frame 1"), ("hall", "Frame 2"),
                                                            ("study", "Frame 3")]
    assert [tuple(row.values()) for row in walls] == [("landing", "Wall 1", ""), ("wall", "Wall 2", "")]

    with pytest.raises(UniqueViolation), registry.db.transaction() as conn:
        conn.execute("UPDATE frames SET name='frame 1' WHERE id='hall'")
