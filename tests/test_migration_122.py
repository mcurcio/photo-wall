"""122 (decision 0034, slice S2f) over rows seeded at the schema before it, in raw SQL as Central wrote
those tables (never through today's code).

Part 1 (122_outputs): one Output row per HDMI port. A port enrolled with a seen Display, its Output
and Frame region documents, a picture document and a picture report is one `outputs` row holding
every value; a port known only by its Output report becomes a row with no observation; the four
tables are gone.

Mutation probe: omit `region_digest` from 122's copy -> the region case fails.
"""
from __future__ import annotations

import json
from uuid import UUID

import pytest
from support.migrations import migrate_before

from central.db import Database

pytestmark = pytest.mark.db

PLAYER = "p-122"
DISPLAY = UUID("00000000-0000-4000-8000-000000000122")
OBSERVATION = {"output_id": "HDMI-A-1", "connected": True, "width_px": 1920, "height_px": 1080}
IDENTITY = {"maker": "SAM", "product": 1, "name": "Samsung", "serial": "S1"}
REPORT = {"output_id": "HDMI-A-1", "connected": True, "current_mode": [1920, 1080]}
REPORT_2 = {"output_id": "HDMI-A-2", "connected": False, "current_mode": None}
DOCUMENT = {"output_id": "HDMI-A-1", "change": 3}
PICTURE_DOCUMENT = {"output_id": "HDMI-A-1", "picture_change": 5}
PICTURE_REPORT = {"output_id": "HDMI-A-1", "offered": None}


def _seed_outputs(conn) -> None:
    conn.execute("INSERT INTO players(id, public_key, token_hash, registered_at, last_seen, device_id) "
                 "VALUES (%s, 'key', 'hash', 1, 2, 'device-122')", (PLAYER,))
    conn.execute("INSERT INTO displays(id, maker, product, name, serial, first_seen_at, matched_lux, panel_black) "
                 "VALUES (%s, 'SAM', 1, 'Samsung', 'S1', 10, 1000.0, 0.0003)", (DISPLAY,))
    conn.execute("INSERT INTO outputs(player_id, output_id, observation) VALUES (%s, 'HDMI-A-1', %s)",
                 (PLAYER, json.dumps(OBSERVATION)))
    conn.execute("INSERT INTO output_displays(player_id, output_id, connected, identity, display_id, report, reported_at) "
                 "VALUES (%s, 'HDMI-A-1', true, %s, %s, %s, 20), (%s, 'HDMI-A-2', false, NULL, NULL, %s, 21)",
                 (PLAYER, json.dumps(IDENTITY), DISPLAY, json.dumps(REPORT), PLAYER, json.dumps(REPORT_2)))
    conn.execute("INSERT INTO output_documents(player_id, output_id, change, digest, document, region_change, "
                 "region_digest) VALUES (%s, 'HDMI-A-1', 3, 'doc-digest', %s, 2, 'region-digest')",
                 (PLAYER, json.dumps(DOCUMENT)))
    conn.execute("INSERT INTO picture_documents(player_id, output_id, picture_change, digest, document) "
                 "VALUES (%s, 'HDMI-A-1', 5, 'picture-digest', %s)", (PLAYER, json.dumps(PICTURE_DOCUMENT)))
    conn.execute("INSERT INTO picture_reports(player_id, output_id, report, reported_at) "
                 "VALUES (%s, 'HDMI-A-1', %s, 30)", (PLAYER, json.dumps(PICTURE_REPORT)))


def test_122_makes_each_port_one_output_row(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "122")
            with db.transaction() as conn:
                _seed_outputs(conn)
            migrate_before(db, "122b")
            with db.transaction() as conn:
                rows = {row["output_id"]: row for row in conn.execute(
                    "SELECT * FROM outputs WHERE player_id=%s", (PLAYER,)).fetchall()}
                tables = {row["tablename"] for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
        finally:
            db.close()

    assert set(rows) == {"HDMI-A-1", "HDMI-A-2"}
    one, two = rows["HDMI-A-1"], rows["HDMI-A-2"]
    assert (one["observation"], one["connected"], one["display_identity"], one["display_id"],
            one["display_report"], one["display_reported_at"]) == (OBSERVATION, True, IDENTITY, DISPLAY, REPORT, 20)
    assert (one["document_change"], one["document_digest"], one["document"]) == (3, "doc-digest", DOCUMENT)
    assert (one["region_change"], one["region_digest"]) == (2, "region-digest")
    assert (one["picture_change"], one["picture_digest"], one["picture_document"]) == (
        5, "picture-digest", PICTURE_DOCUMENT)
    assert (one["picture_report"], one["picture_reported_at"]) == (PICTURE_REPORT, 30)
    assert (one["created_at"], one["created_by"], one["changed_at"], one["changed_by"]) == (20, "unknown", 30, "unknown")
    # A port heard only in its Output report: a row, no observation, no documents.
    assert (two["observation"], two["connected"], two["display_report"], two["document"]) == (
        None, False, REPORT_2, None)
    assert not tables & {"output_displays", "output_documents", "picture_documents", "picture_reports"}


def test_after_122_central_has_80_tables(registry):
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='public' "
                            "AND tablename NOT LIKE 'procrastinate%%'").fetchone()["n"] == 80
