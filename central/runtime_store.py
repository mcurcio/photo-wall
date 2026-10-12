"""Transactional current Runtime ownership; never persist a future projection."""

from contextlib import contextmanager

from psycopg.types.json import Jsonb

from central.db import Database
from central.infra.house_store import house_time_in
from central.runtime import Runtime
from central.transaction_locks import RUNTIME_LOCK as RUNTIME_LOCK
from central.transaction_locks import acquire_runtime_locks
from contracts.time import Clock

_RUNTIME_COMMANDS = frozenset({
    "set_scene", "delete_scene", "create_program", "save_program", "remove_program",
    "set_default_scene", "activate", "finish", "cancel", "advance",
})
_TIMED_COMMANDS = frozenset({
    "create_program", "save_program", "remove_program", "set_default_scene",
    "activate", "finish", "cancel", "advance",
})


class RuntimeStore:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    @contextmanager
    def edit(self, conn=None):
        if conn is None:
            with self.db.transaction() as transaction, self.edit(transaction) as runtime:
                yield runtime
            return
        acquire_runtime_locks(conn)
        runtime = self.read_in(conn)
        yield runtime
        conn.execute(
            "INSERT INTO runtime_state(singleton,revision,snapshot,updated_at) VALUES(TRUE,1,%s,%s) "
            "ON CONFLICT(singleton) DO UPDATE SET revision=runtime_state.revision+1,"
            "snapshot=EXCLUDED.snapshot,updated_at=EXCLUDED.updated_at",
            (Jsonb(runtime.export_state()), self.clock.utc()),
        )

    def read(self) -> Runtime:
        with self.db.transaction() as conn:
            return self.read_locked(conn)

    @staticmethod
    def read_in(conn) -> Runtime:
        """Restore one MVCC cut of Runtime in a caller transaction, with the house time read in
        the same transaction (a Program's windows follow the house's zone and place)."""
        house = house_time_in(conn)
        row = conn.execute("SELECT snapshot FROM runtime_state WHERE singleton").fetchone()
        return Runtime.restore(row["snapshot"], house=house) if row is not None else Runtime(house=house)

    def read_locked(self, conn) -> Runtime:
        """Read current state while serializing with Runtime writers, without saving it."""
        acquire_runtime_locks(conn)
        return self.read_in(conn)

    def command(self, method: str, *args, **kwargs):
        # The HTTP adapter cannot call arbitrary object methods through operator input.
        if method not in _RUNTIME_COMMANDS:
            raise ValueError("unknown Runtime command")
        with self.edit() as runtime:
            return getattr(runtime, method)(*args, **kwargs)

    def command_current(self, method: str, *args, **kwargs):
        """Apply a wall-clock command at the post-lock serialization cut.

        Explicit-time ``command`` remains available for controllable-clock
        simulation. Operator routes must use this path so a concurrent writer
        cannot advance persisted Runtime past a time sampled before lock wait.
        """
        if method not in _TIMED_COMMANDS or "now" in kwargs:
            raise ValueError("unknown current-time Runtime command")
        with self.edit() as runtime:
            return getattr(runtime, method)(*args, self.clock.utc(), **kwargs)
