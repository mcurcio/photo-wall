"""Transactional current Runtime ownership; never persist a future projection."""

from contextlib import contextmanager

from psycopg.types.json import Jsonb

from central.db import Database
from central.runtime import Runtime
from contracts.time import Clock

RUNTIME_LOCK = 734118323


class RuntimeStore:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    @contextmanager
    def edit(self, conn=None):
        if conn is None:
            with self.db.transaction() as transaction, self.edit(transaction) as runtime:
                yield runtime
            return
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (RUNTIME_LOCK,))
        row = conn.execute("SELECT * FROM runtime_state WHERE singleton").fetchone()
        runtime = Runtime.restore(row["snapshot"]) if row else Runtime()
        yield runtime
        conn.execute(
            "INSERT INTO runtime_state(singleton,revision,snapshot,updated_at) VALUES(TRUE,1,%s,%s) "
            "ON CONFLICT(singleton) DO UPDATE SET revision=runtime_state.revision+1,"
            "snapshot=EXCLUDED.snapshot,updated_at=EXCLUDED.updated_at",
            (Jsonb(runtime.export_state()), self.clock.utc()),
        )

    def read(self) -> Runtime:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT snapshot FROM runtime_state WHERE singleton").fetchone()
        return Runtime.restore(row["snapshot"]) if row else Runtime()

    def command(self, method: str, *args, **kwargs):
        # The HTTP adapter cannot call arbitrary object methods through operator input.
        if method not in {"set_scene", "set_program", "remove_program", "activate", "finish", "cancel", "advance"}:
            raise ValueError("unknown Runtime command")
        with self.edit() as runtime:
            return getattr(runtime, method)(*args, **kwargs)
