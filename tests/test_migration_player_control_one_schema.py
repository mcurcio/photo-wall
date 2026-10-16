"""Migration 076 leaves one control schema: rows a legacy session sealed, or that negotiated
schema 1, reopen so their Player's next hello negotiates; a schema-2 negotiation stands; the
per-session schema column and the older pods' nonce guard trigger are gone.

One database at 074 holds three enrolled Players (through the real Registry), their control rows
put in the pre-076 shapes by SQL; `Database.migrate()` then applies 076. Real PostgreSQL; skips
without PHOTO_WALL_TEST_DATABASE_URL (CI runs it).
"""

from __future__ import annotations

import pytest
from content_db import schema_before
from psycopg.types.json import Jsonb
from test_registry import enroll

from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.frame_lighting_store import PgFrameLighting
from central.registry import Registry
from contracts.time import ManualClock

NONCE = "d" * 64


@pytest.fixture
def before_076(empty_database):
    with schema_before(empty_database, "076") as db:
        yield Registry(db, ManualClock(1000), lighting=PgFrameLighting(LUT_RECIPE_ID))


def test_076_reopens_legacy_and_schema_1_rows_and_keeps_schema_2(before_076):
    """Mutation probe: `WHERE false` on the UPDATE (the legacy and schema-1 rows stay sealed,
    and the status CHECK refuses the legacy row)."""
    registry = before_076
    players = [enroll(registry)[0]["player_id"] for _ in range(3)]
    legacy, schema_1, schema_2 = players
    with registry.db.transaction() as conn:
        conn.execute("UPDATE player_control_sessions SET status='legacy' WHERE player_id=%s",
                     (legacy,))
        conn.execute("UPDATE player_control_sessions SET status='negotiated',schema_version=1,"
                     "capabilities=%s,offered_schemas=%s,offered_capabilities=%s "
                     "WHERE player_id=%s",
                     (Jsonb([]), Jsonb([1]), Jsonb([]), schema_1))
        # A schema-2 row with an applied ACK, as Registry leaves it after a matching receipt.
        conn.execute("UPDATE player_control_sessions SET status='negotiated',schema_version=2,"
                     "capabilities=%s,offered_schemas=%s,offered_capabilities=%s,"
                     "issued_sequence=1,applied_sequence=1,applied_at=1,"
                     "applied_delivery_id=%s,applied_digest=%s,last_delivery_id=%s,"
                     "last_result_sequence=1,last_result_digest=%s,last_result='applied',"
                     "last_result_at=1,applied_ack_nonce=%s WHERE player_id=%s",
                     (Jsonb(["identify_output"]), Jsonb([2]), Jsonb(["identify_output"]),
                      "1" * 32, "e" * 64, "1" * 32, "e" * 64, NONCE, schema_2))

    registry.db.migrate()

    with registry.db.transaction() as conn:
        rows = {row["player_id"]: row for row in conn.execute(
            "SELECT player_id,status,capabilities,offered_schemas,offered_capabilities,"
            "applied_ack_nonce FROM player_control_sessions").fetchall()}
        columns = {row["column_name"] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='player_control_sessions'").fetchall()}
        triggers = conn.execute("SELECT count(*) AS n FROM pg_trigger "
                                "WHERE tgname='player_control_ack_nonce_guard'").fetchone()
        functions = conn.execute("SELECT count(*) AS n FROM pg_proc "
                                 "WHERE proname='player_control_invalidate_ack_nonce'").fetchone()
    reopened = {"status": "open", "capabilities": [], "offered_schemas": None,
                "offered_capabilities": None, "applied_ack_nonce": None}
    assert {key: rows[legacy][key] for key in reopened} == reopened
    assert {key: rows[schema_1][key] for key in reopened} == reopened
    assert {key: rows[schema_2][key] for key in reopened} == {
        "status": "negotiated", "capabilities": ["identify_output"], "offered_schemas": [2],
        "offered_capabilities": ["identify_output"], "applied_ack_nonce": NONCE}
    assert "schema_version" not in columns
    assert triggers["n"] == 0 and functions["n"] == 0
