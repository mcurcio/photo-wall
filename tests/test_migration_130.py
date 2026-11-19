"""130 (decision 0034: audit fields on every kept table) over rows seeded at the schema before it, in
raw SQL as Central wrote `home_assistant_entities` (never through today's code).

Each kept entity gains its audit fields, created and changed when it was last heard, by
`home-assistant`, its one writer; the columns are required from then on.

Mutation probe: leave out 130's UPDATE -> the SET NOT NULL fails on the kept row.
"""
from __future__ import annotations

import psycopg
import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db


def test_130_stamps_each_kept_entity_when_it_was_last_heard(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "130")
            with db.transaction() as conn:
                conn.execute("INSERT INTO home_assistant_entities(entity, kind, name, heard_at) "
                             "VALUES ('light.hall', 'light', 'Hall', to_timestamp(1763500000))")
            db.migrate()
            with db.transaction() as conn:
                row = conn.execute("SELECT created_at, created_by, changed_at, changed_by "
                                   "FROM home_assistant_entities").fetchone()
                with pytest.raises(psycopg.errors.NotNullViolation):
                    conn.execute("INSERT INTO home_assistant_entities(entity, kind, name, heard_at) "
                                 "VALUES ('light.den', 'light', 'Den', clock_timestamp())")
        finally:
            db.close()

    assert row == {"created_at": 1763500000.0, "created_by": "home-assistant",
                   "changed_at": 1763500000.0, "changed_by": "home-assistant"}
