"""101 over a database as 100 left it: every Frame hangs at turn 0, a positioned, ready Frame stays
exactly as it was, and every stored Output document decodes under the display line's schema major 9
(render redesign S2; the migration moves no generation, so no ready Frame needs its Position again,
and there is no backwards-compatible decoder). A Cover Position whose own rotation is a quarter turn
keeps it: migration 102 (S5) moves that quarter turn to the Frame's turn when it converts the
Position.

The rows are seeded through today's code on the whole schema, then 101 is undone on the real tables
(`before_101`: the column dropped, each stored document as 100 stored it, without a turn and under a
digest made without one), as test_migration_092 does: today's seeding reads frames.turn, so it
cannot run on a pre-101 schema. The Cover Position is raw JSON, as 100 stored it (no model of
today's), and the first test applies migrations through 101 only: 102 converts that Position.
"""
from __future__ import annotations

import json

from psycopg.types.json import Jsonb
from support.frames import frame_create
from support.migrations import migrate_before
from test_display_identity import enroll_pi

from central.content_catalog.catalog import device_id_for_serial
from central.displays.model import Readiness
from central.infra.display_store import frame_readiness_in, output_documents
from contracts.node_output import decode_output_document

SERIAL = "serial-migration-101"
COVER_AT_90 = {"revision": 2, "fit": {"mode": "cover", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "rotation": 90}}
PRE_101_DIGEST = "a digest made without the turn"


def before_101(registry) -> None:
    """Undo 101 on the real tables: no frames.turn, each stored Output document without its turn
    (as 100's source wrote it, under a digest of that body), and 101 not in the ledger."""
    with registry.db.transaction() as conn:
        conn.execute("ALTER TABLE frames DROP COLUMN turn")
        conn.execute("UPDATE output_documents SET document = document - 'turn', digest = %s", (PRE_101_DIGEST,))
        conn.execute("DELETE FROM schema_migrations WHERE name = '101_frame_turn.sql'")


def _frame(registry) -> dict:
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT generation, configuration_revision, calibration FROM frames WHERE id='kitchen'"
                           ).fetchone()
        return {**row, "readiness": frame_readiness_in(conn, "kitchen")}


def _stored(registry) -> dict[str, dict]:
    with registry.db.transaction() as conn:
        return {row["output_id"]: row for row in conn.execute(
            "SELECT output_id, change, document FROM output_documents ORDER BY output_id").fetchall()}


def test_101_hangs_every_frame_at_0_and_keeps_a_ready_frame_ready(registry):
    player = enroll_pi(registry, SERIAL)
    registry.create_frame(frame_create("kitchen"))
    registry.bind("kitchen", player, "HDMI-A-1", expected_generation=0)
    with registry.db.transaction() as conn:   # a saved Cover Position at 90, as 100 stored it
        conn.execute("UPDATE frames SET calibration=%s, position_generation=generation WHERE id='kitchen'",
                     (Jsonb(COVER_AT_90),))
    before = _frame(registry)
    assert (before["readiness"], before["calibration"]) == (Readiness.READY, COVER_AT_90)
    before_101(registry)

    migrate_before(registry.db, "102")

    assert _frame(registry) == before
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT turn FROM frames WHERE id='kitchen'").fetchone()["turn"] == 0


def test_101_rewrites_every_stored_output_document_to_schema_9(registry):
    """Every stored Output document, bound or not, decodes under schema 9 with turn 0 and keeps its
    change; the source's next read sends it under the next change (its digest was made without the
    turn). Mutation probe: drop 101's `UPDATE output_documents` -> the stored documents do not
    decode (ValueError output_document) and this test fails."""
    player = enroll_pi(registry, SERIAL)
    registry.create_frame(frame_create("kitchen"))
    registry.bind("kitchen", player, "HDMI-A-1", expected_generation=0)
    device = device_id_for_serial(SERIAL)
    with registry.db.transaction() as conn:   # the source's read stores each Output's document
        output_documents(conn, device, 1000.0)
    changes = {output_id: row["change"] for output_id, row in _stored(registry).items()}
    assert set(changes) == {"HDMI-A-1", "HDMI-A-2"}
    before_101(registry)
    assert all("turn" not in row["document"] for row in _stored(registry).values())

    registry.db.migrate()

    for output_id, row in _stored(registry).items():
        document = decode_output_document(json.dumps(row["document"]).encode())
        assert (document.output_id, document.change, document.turn) == (output_id, changes[output_id], 0)
    with registry.db.transaction() as conn:
        sent = output_documents(conn, device, 1000.0)
    assert {key: decode_output_document(value).change for key, value in sent.items() if key.startswith("output-")} == {
        f"output-{output_id}": change + 1 for output_id, change in changes.items()}
