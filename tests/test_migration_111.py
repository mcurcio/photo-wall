"""111 over rows seeded at 110: every kept "can't tell since" becomes a "cant-tell" doubt since then,
a reference that tells keeps no doubt, and Galleria's stored birth (an older Galleria's, naming no
hub connection, which no longer reads) is removed, so the Home Assistant page reads none until
Galleria puts its birth again."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from central.db import Database
from central.infra.room_light_store import galleria_birth_in
from central.room_light.model import Doubt, DoubtReason

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
HA = "home-assistant"


def _at_110(db: Database) -> None:
    """Every migration up to 110, as Database.migrate applies them."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                     "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= "111":
                break
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                         (path.name, sha256(path.read_bytes()).hexdigest()))


def _doubts(conn, table: str) -> dict[str, Doubt | None]:
    rows = conn.execute(f"SELECT address, doubt_reason, EXTRACT(EPOCH FROM doubt_since) AS since FROM {table}")  # noqa: S608
    return {row["address"]: None if row["doubt_reason"] is None else Doubt(DoubtReason(row["doubt_reason"]),
                                                                          float(row["since"])) for row in rows}


def test_111_turns_can_t_tell_into_a_doubt_and_drops_an_old_galleria_birth(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo)
        try:
            _at_110(db)
            with db.transaction() as conn:
                conn.execute("INSERT INTO light_words(source, address, heard_at, cant_tell_since) VALUES "
                             "(%s, 'light.away', to_timestamp(300), to_timestamp(300)), "
                             "(%s, 'light.here', to_timestamp(400), NULL)", (HA, HA))
                conn.execute("INSERT INTO reading_words(source, address, heard_at, cant_tell_since) VALUES "
                             "(%s, 'sensor.gone', to_timestamp(500), to_timestamp(450))", (HA,))
                conn.execute("INSERT INTO colour_words(source, address, heard_at, cant_tell_since, created_at, "
                             "created_by, changed_at, changed_by) VALUES (%s, 'sensor.sky', to_timestamp(600), "
                             "to_timestamp(550), now(), %s, now(), %s)", (HA, HA, HA))
                conn.execute("INSERT INTO galleria_birth(one, birth, heard_at, created_at, created_by, changed_at, "
                             "changed_by) VALUES (true, %s, now(), now(), 'galleria', now(), 'galleria')",
                             (json.dumps({"component": "galleria", "version": "0.43.0", "kinds": ["light"]}),))
            db.migrate()
            with db.transaction() as conn:
                assert _doubts(conn, "light_words") == {"light.away": Doubt(DoubtReason.CANT_TELL, 300.0),
                                                        "light.here": None}
                assert _doubts(conn, "reading_words") == {"sensor.gone": Doubt(DoubtReason.CANT_TELL, 450.0)}
                assert _doubts(conn, "colour_words") == {"sensor.sky": Doubt(DoubtReason.CANT_TELL, 550.0)}
                assert galleria_birth_in(conn) is None
                for table, address, heard in (("light_words", "light.away", 300.0), ("reading_words", "sensor.gone", 500.0)):
                    row = conn.execute(f"SELECT created_by, changed_by, EXTRACT(EPOCH FROM created_at) AS created, "  # noqa: S608
                                       f"EXTRACT(EPOCH FROM changed_at) AS changed FROM {table} WHERE address=%s",
                                       (address,)).fetchone()
                    assert (row["created_by"], row["changed_by"], float(row["created"]), float(row["changed"])) == (
                        HA, HA, heard, heard)   # audit fields backfilled: its source, when last heard
        finally:
            db.close()
