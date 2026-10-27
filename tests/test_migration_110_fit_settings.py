"""110 over a photo fit settings row saved at 109: the Player's still limit (`max_still_edge`, no
longer a setting) leaves the row and every other saved value stays, so the house's settings are
not reset to their defaults (a row naming an unknown field no longer validates and reads as the
defaults: central/infra/fit_settings_store.py)."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from central.db import Database
from central.infra.fit_settings_store import PgFitSettings

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"


def _at_109(db: Database) -> None:
    """Every migration up to 109, as Database.migrate applies them."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                     "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= "110":
                break
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                         (path.name, sha256(path.read_bytes()).hexdigest()))


def test_110_drops_the_still_limit_and_keeps_the_saved_settings(database_provisioner):
    """Mutation probe AC5 (an empty 110): the row no longer validates and reads as the defaults."""
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo)
        try:
            _at_109(db)
            with db.transaction() as conn:
                conn.execute("INSERT INTO photo_fit_settings(singleton, settings, updated_at) VALUES (TRUE, %s, 0)",
                             (json.dumps({"crop_limit": 0.5, "max_still_edge": 3000}),))
            db.migrate()
            with db.transaction() as conn:
                settings = PgFitSettings.fit_settings_in(conn)
                stored = conn.execute("SELECT settings FROM photo_fit_settings").fetchone()["settings"]
            assert settings.crop_limit == 0.5
            assert settings.scaled_copy_ratio == 1.5
            assert "max_still_edge" not in stored
        finally:
            db.close()
