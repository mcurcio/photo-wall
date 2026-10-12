"""The photo fit settings over PostgreSQL (follow-ups 2026-10-11, slice B1; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

`PgFitSettings` implements central.wall_plan.ports.FitSettingsStore and is the one home of
migration 086's `photo_fit_settings` table: one row holding a complete `FitSettings`, validated
by that model alone (the one home of the ranges). No row reads as `FitSettings()` (today's
values), so correctness holds with the table empty. A row that no longer validates (a hand edit)
reads as the defaults and is logged, never a crash.
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from central.db import Database, TransactionClock
from central.wall_plan.fit_settings import FitSettings

LOG = logging.getLogger("central.fit_settings")


class PgFitSettings:
    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` stamps `updated_at` (the database's clock in production, G11)."""
        self.db, self.times = db, times

    @staticmethod
    def fit_settings_in(conn: Any) -> FitSettings:
        """FitSettingsStore.fit_settings_in."""
        row = conn.execute("SELECT settings FROM photo_fit_settings WHERE singleton").fetchone()
        if row is None:
            return FitSettings()
        try:
            return FitSettings.model_validate(row["settings"])
        except ValidationError:
            LOG.warning("photo_fit_settings holds values outside their ranges; using defaults")
            return FitSettings()

    def fit_settings(self) -> FitSettings:
        """FitSettingsStore.fit_settings."""
        with self.db.transaction() as conn:
            return self.fit_settings_in(conn)

    def save_fit_settings(self, settings: FitSettings) -> None:
        """FitSettingsStore.save_fit_settings: the row replaced, last write wins."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO photo_fit_settings(singleton,settings,updated_at) VALUES(TRUE,%s,%s) "
                "ON CONFLICT (singleton) DO UPDATE SET settings=EXCLUDED.settings,"
                "updated_at=EXCLUDED.updated_at",
                (Jsonb(settings.model_dump(mode="json")), self.times.now_in(conn)))
