"""122 (decision 0034, slice S2f) over rows seeded at the schema before it, in raw SQL as Central wrote
those tables (never through today's code).

Part 1 (122_outputs): one Output row per HDMI port. A port enrolled with a seen Display, its Output
and Frame region documents, a picture document and a picture report is one `outputs` row holding
every value; a port known only by its Output report becomes a row with no observation; the four
tables are gone.

Part 2 (122b_displays_and_picture_history): a Display's open picture session is on its row, the
shared-serial fact a Display flag (lost for a triple with no Display), every kept and sent picture
entry one `picture_history` row keeping its id and kind; displays and power_requests gain their
audit fields; the four tables are gone.

Mutation probes: omit `region_digest` from 122's copy -> the region case fails; copy the sent
entries without their `test_pattern` -> the history case fails.
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


FRAME = "f-122"
SESSION = UUID("00000000-0000-4000-8000-0000000000a1")
KEPT = UUID("00000000-0000-4000-8000-0000000000b1")
SENT = UUID("00000000-0000-4000-8000-0000000000c1")
DRAFT = {"hardware_picture": {"10": 40}, "software_contrast_percent": 110, "colour_temperature_kelvin": 6500,
         "gamma_tenths": 22, "signal_range": "auto", "matched_lux": 1000.0, "panel_black": 0.0003}


def _seed_display_history(conn) -> None:
    conn.execute("INSERT INTO walls(surface_id, name) VALUES ('w-122', 'Wall') ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO frames(id, surface_id, x_mm, y_mm, calibration, name, lighting) "
                 "VALUES (%s, 'w-122', 0, 0, '{}', 'Frame', '{}')", (FRAME,))
    conn.execute("INSERT INTO picture_sessions(id, display_id, frame_id, sequence, draft, idle_until, created_at, "
                 "began_at_change, hardware_before, touched_hardware, touched_software, test_pattern, "
                 "test_pattern_ask) VALUES (%s, %s, %s, 3, %s, 500, 400, 5, '{\"10\": 60}', '{10}', "
                 "'{contrast}', 'white', 2)", (SESSION, DISPLAY, FRAME, json.dumps(DRAFT)))
    conn.execute("INSERT INTO shared_serials(maker, product, serial, found_at) VALUES ('SAM', 1, 'S1', 50), "
                 "('SAM', 1, 'nobody', 51)")
    conn.execute("INSERT INTO picture_kept_entries(id, display_id, number, kind, at, picture) "
                 "VALUES (%s, %s, 7, 'kept-done', to_timestamp(600), %s)", (KEPT, DISPLAY, json.dumps(DRAFT)))
    conn.execute("INSERT INTO picture_sent_entries(id, player_id, output_id, picture_change, display_id, frame_id, "
                 "kind, at, session_id, picture, test_pattern) VALUES (%s, %s, 'HDMI-A-1', 5, %s, %s, 'sent-draft', "
                 "to_timestamp(610), %s, %s, 'white')", (SENT, PLAYER, DISPLAY, FRAME, SESSION, json.dumps(DRAFT)))
    conn.execute("INSERT INTO power_requests(id, frame_id, power, reason, for_seconds, created_at, ends_at) "
                 "VALUES (gen_random_uuid(), %s, 'off', 'console-test', 30, 700, 730)", (FRAME,))


def test_122b_puts_the_session_and_the_flag_on_the_display_and_the_entries_in_one_history(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            migrate_before(db, "122")
            with db.transaction() as conn:
                _seed_outputs(conn)
                _seed_display_history(conn)
            db.migrate()
            with db.transaction() as conn:
                [display] = conn.execute("SELECT * FROM displays").fetchall()
                history = {row["id"]: row for row in conn.execute("SELECT * FROM picture_history").fetchall()}
                [power] = conn.execute("SELECT * FROM power_requests").fetchall()
                tables = {row["tablename"] for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
        finally:
            db.close()

    session = display["picture_session"]
    assert (session["id"], session["frame_id"], session["sequence"], session["draft"], session["idle_until"],
            session["created_at"], session["began_at_change"], session["hardware_before"],
            session["touched_hardware"], session["touched_software"], session["test_pattern"],
            session["test_pattern_ask"], session["restored_from"]) == (
        str(SESSION), FRAME, 3, DRAFT, 500, 400, 5, {"10": 60}, ["10"], ["contrast"], "white", 2, None)
    assert display["shared_serial"] is True
    assert (display["created_at"], display["created_by"], display["changed_at"], display["changed_by"]) == (
        10, "unknown", 10, "unknown")
    kept, sent = history[KEPT], history[SENT]
    assert (kept["kind"], kept["entry_kind"], kept["display_id"], kept["number"], kept["by"], kept["picture"]) == (
        "kept", "kept-done", DISPLAY, 7, "unknown", DRAFT)
    assert (sent["kind"], sent["entry_kind"], sent["player_id"], sent["output_id"], sent["picture_change"],
            sent["frame_id"], sent["session_id"], sent["test_pattern"], sent["by"]) == (
        "sent", "sent-draft", PLAYER, "HDMI-A-1", 5, FRAME, SESSION, "white", "unknown")
    assert (power["created_by"], power["changed_at"], power["changed_by"]) == ("unknown", 700, "unknown")
    assert not tables & {"picture_sessions", "shared_serials", "picture_kept_entries", "picture_sent_entries"}


def test_after_122b_central_has_77_tables(registry):
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='public' "
                            "AND tablename NOT LIKE 'procrastinate%%'").fetchone()["n"] == 77
