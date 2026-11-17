"""The photo fit settings over PostgreSQL (follow-ups 2026-10-11, slice B1; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

`PgFitSettings` implements central.wall_plan.ports.FitSettingsStore and is the one home of the
`singletons` document 'photo_fit' (decision 0034's migration 118): a complete `FitSettings`,
validated by that model alone (the one home of the ranges). No document reads as `FitSettings()`
(today's values), so correctness holds before the first save. A document that no longer
validates (a hand edit) reads as the defaults and is logged, never a crash (`Singleton.read_in`).
"""
from __future__ import annotations

from typing import Any, Final

from central.actor import Actor
from central.db import Database, TransactionClock
from central.infra.singletons import Singleton
from central.wall_plan.fit_settings import FitSettings

PHOTO_FIT: Final[Singleton[FitSettings]] = Singleton("photo_fit", FitSettings)


class PgFitSettings:
    def __init__(self, db: Database, *, times: TransactionClock) -> None:
        """`times` stamps the document's audit times (the database's clock in production, G11)."""
        self.db, self.times = db, times

    @staticmethod
    def fit_settings_in(conn: Any) -> FitSettings:
        """FitSettingsStore.fit_settings_in."""
        stored = PHOTO_FIT.read_in(conn)
        return FitSettings() if stored is None else stored.value

    def fit_settings(self) -> FitSettings:
        """FitSettingsStore.fit_settings."""
        with self.db.transaction() as conn:
            return self.fit_settings_in(conn)

    def save_fit_settings(self, settings: FitSettings, *, by: Actor) -> None:
        """FitSettingsStore.save_fit_settings: the document replaced, last write wins; an equal
        save writes nothing."""
        with self.db.transaction() as conn:
            PHOTO_FIT.write_in(conn, settings, by=by, times=self.times)
