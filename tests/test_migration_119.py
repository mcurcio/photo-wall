"""119 (decision 0034, slice S2c): the seven bus tables become `node_links`, `node_states` and
`events`, and the WALL mark the `singletons` document 'wall_mark'.

The migration's SQL runs on a test database at head after dropping 119's three tables, deleting
'wall_mark' and recreating the seven tables as 118 left them, seeded with two devices.

Mutation probe (S2c #1): drop `DISTINCT ON` from 119's node_states copy -> the copy violates the
primary key (or keeps an older value).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from central.infra.node_link_store import WallMark

pytestmark = pytest.mark.db

MIGRATION = Path(__file__).parents[1] / "central/migrations/119_bus_store.sql"
DROPPED = ("node_link_records", "node_link_gaps", "node_link_cursors", "node_link_documents",
           "node_link_actions", "node_bus_presence", "node_bus_wall")
A, B = "device-a", "device-b"
STATE = "KV_state_display"
RECORDS = "RECORD_display"
AT_118 = (Path(__file__).parents[1] / "central/migrations/066_node_link_records.sql").read_text() \
    + (Path(__file__).parents[1] / "central/migrations/067_node_bus_wall.sql").read_text() \
    + """
CREATE TABLE node_bus_presence (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    linked BOOLEAN NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL);
"""


def _at_118(conn) -> None:
    conn.execute("DROP TABLE node_links, node_states, events")
    conn.execute("DELETE FROM singletons WHERE name='wall_mark'")
    conn.execute(AT_118)
    for device in (A, B):
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device, device + "-serial"))
    records = "INSERT INTO node_link_records VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    for device in (A, B):
        for key in ("x", "y"):
            for seq, at in ((1, 10.0), (2, 30.0), (3, 20.0)):   # seq 2 is the newest by record time
                conn.execute(records, (device, STATE, "e1", seq * 10 + ord(key), f"$KV.state_display.{key}",
                                       None, Jsonb({}), f"{key}{seq}".encode(), at))
    conn.execute(records, (A, STATE, "e1", 500, "$KV.state_display.z", None,
                           Jsonb({"KV-Operation": "DEL"}), b"", 40.0))
    for seq in (1, 2, 3):
        conn.execute(records, (A, RECORDS, "e1", seq, "record.shown", f"m{seq}", Jsonb({"h": "v"}),
                               b"r%d" % seq, 5.0 + seq))
    conn.execute("INSERT INTO node_link_gaps VALUES (%s, %s, 'e1', 3, 7, 50), (%s, %s, 'e0', 9, NULL, 49)",
                 (A, RECORDS, B, RECORDS))
    conn.execute("INSERT INTO node_link_actions (device_id, pipe, kind, body, recorded_at) VALUES "
                 "(%s, 'fleet', 'call', %s, 60), (%s, 'show', 'document-refused', %s, 61)",
                 (A, Jsonb({"method": "m"}), B, Jsonb({"key": "k"})))
    conn.execute("INSERT INTO node_link_cursors VALUES (%s, %s, 'fleet', 'e1', 3), "
                 "(%s, 'KV_state_content', 'show', 'e2', 4)", (A, RECORDS, A))
    conn.execute("INSERT INTO node_link_documents VALUES (%s, 'KV_desired_display', 'show', 'dg', 'e5', 2), "
                 "(%s, 'KV_desired_display', 'other', 'dh', 'e5', 3)", (A, A))
    conn.execute("INSERT INTO node_bus_presence VALUES (%s, false, 77.5)", (B,))
    conn.execute("INSERT INTO node_bus_wall (mark) VALUES (42)")


def test_119_moves_the_bus_store_into_three_tables(registry):
    with registry.db.transaction() as conn:
        _at_118(conn)
        conn.execute(MIGRATION.read_text())
        for name in DROPPED:
            assert conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is None, name
        states = {(row["device_id"], row["subject"]): (row["seq"], row["removed"], bytes(row["data"]))
                  for row in conn.execute("SELECT * FROM node_states").fetchall()}
        events = conn.execute("SELECT kind, device_id, stream, epoch, seq, message_id, body, data,"
                              " recorded_at, recorded_by FROM events ORDER BY id").fetchall()
        links = {row["device_id"]: row for row in conn.execute("SELECT * FROM node_links").fetchall()}
        wall = conn.execute("SELECT document, created_by FROM singletons WHERE name='wall_mark'").fetchone()

    assert states == {
        **{(device, f"$KV.state_display.{key}"): (20 + ord(key), False, f"{key}2".encode())
           for device in (A, B) for key in ("x", "y")},
        (A, "$KV.state_display.z"): (500, True, b"")}
    records = [row for row in events if row["kind"] == "record"]
    assert [(r["device_id"], r["stream"], r["seq"], r["message_id"], bytes(r["data"]), r["recorded_by"])
            for r in records] == [(A, RECORDS, seq, f"m{seq}", b"r%d" % seq, "node") for seq in (1, 2, 3)]
    gaps = sorted((r["device_id"], r["epoch"], r["seq"], r["body"]["lost"]) for r in events if r["kind"] == "gap")
    assert gaps == [(A, "e1", 3, 7), (B, "e0", 9, None)]
    actions = [(r["device_id"], r["body"], r["recorded_at"], r["recorded_by"])
               for r in events if r["kind"] == "link-action"]
    assert actions == [(A, {"pipe": "fleet", "kind": "call", "body": {"method": "m"}}, 60, "central"),
                       (B, {"pipe": "show", "kind": "document-refused", "body": {"key": "k"}}, 61, "central")]
    assert set(links) == {A, B}
    assert links[A]["cursors"] == {RECORDS: {"pipe": "fleet", "epoch": "e1", "seq": 3},
                                   "KV_state_content": {"pipe": "show", "epoch": "e2", "seq": 4}}
    assert links[A]["documents"] == {"KV_desired_display": {"show": {"digest": "dg", "epoch": "e5", "seq": 2},
                                                            "other": {"digest": "dh", "epoch": "e5", "seq": 3}}}
    assert (links[A]["linked"], links[A]["linked_changed_at"], links[A]["created_by"]) == (None, None, "unknown")
    assert (links[B]["cursors"], links[B]["documents"]) == ({}, {})
    assert (links[B]["linked"], links[B]["linked_changed_at"]) == (False, 77.5)
    assert WallMark.model_validate(wall["document"]) == WallMark(mark=42) and wall["created_by"] == "unknown"


def test_119_with_nothing_recorded_writes_nothing(registry):
    with registry.db.transaction() as conn:
        conn.execute("DROP TABLE node_links, node_states, events")
        conn.execute("DELETE FROM singletons WHERE name='wall_mark'")
        conn.execute(AT_118)
        conn.execute(MIGRATION.read_text())
        assert conn.execute("SELECT (SELECT count(*) FROM node_links) + (SELECT count(*) FROM node_states)"
                            " + (SELECT count(*) FROM events) AS n").fetchone()["n"] == 0
        assert conn.execute("SELECT 1 FROM singletons WHERE name='wall_mark'").fetchone() is None
