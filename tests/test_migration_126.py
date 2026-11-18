"""126 (decision 0034, S2f's erratum) over rows seeded at the schema before it, in raw SQL as Central
wrote `position_entries` (never through today's code).

Every Position entry says who appended it: the entries kept before 126 did not record who, so they
read 'unknown', and an entry appended without `by` is refused.

Mutation probes: leave out the UPDATE -> the NOT NULL refuses the migration; leave out the NOT NULL
-> the INSERT without `by` is taken.
"""
from __future__ import annotations

from uuid import UUID

import psycopg
import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db

ENTRIES = (UUID(int=1), UUID(int=2))


def _seed(conn) -> None:
    conn.execute("INSERT INTO frames(id, name, surface_id, x_mm, y_mm, calibration, lighting, created_at, "
                 "created_by, changed_at, changed_by) VALUES ('kitchen', 'Kitchen', 'wall', 0, 0, '{}', '{}', "
                 "1, 'console', 1, 'console')")
    conn.execute("INSERT INTO position_entries(id, frame_id, number, kind, at, fit, lighting, turn) VALUES "
                 "(%s, 'kitchen', 1, 'kept-created', '2026-11-01T00:00:00Z', '{}', '{}', 0), "
                 "(%s, 'kitchen', 2, 'kept-saved', '2026-11-02T00:00:00Z', '{}', '{}', 0)", ENTRIES)


def test_126_says_who_appended_every_position_entry(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "126")
            with db.transaction() as conn:
                _seed(conn)
            db.migrate()
            with db.transaction() as conn:
                entries = {row["id"]: row["by"] for row in conn.execute(
                    "SELECT id, by FROM position_entries").fetchall()}
            with pytest.raises(psycopg.errors.NotNullViolation), db.transaction() as conn:
                conn.execute("INSERT INTO position_entries(id, frame_id, number, kind, at, fit, lighting, turn) "
                             "VALUES (%s, 'kitchen', 3, 'kept-saved', now(), '{}', '{}', 0)", (UUID(int=3),))
        finally:
            db.close()

    assert entries == dict.fromkeys(ENTRIES, "unknown")
