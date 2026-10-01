"""Transactional current Runtime ownership; never persist a future projection."""

import re
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from central.db import Database
from central.runtime import AdmissionPolicy, Runtime
from central.transaction_locks import RUNTIME_LOCK as RUNTIME_LOCK
from central.transaction_locks import acquire_runtime_locks
from contracts.models import TARGET_ID_PATTERN
from contracts.time import Clock

_RUNTIME_COMMANDS = frozenset({
    "set_scene", "delete_scene", "set_program", "replace_program", "remove_program",
    "activate", "finish", "cancel", "advance",
})
_TIMED_COMMANDS = frozenset({
    "replace_program", "remove_program", "activate", "finish", "cancel", "advance",
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
        """Restore one MVCC cut of Runtime and active barriers in a caller transaction."""
        row = conn.execute(
            "SELECT (SELECT snapshot FROM runtime_state WHERE singleton) AS snapshot,"
            "COALESCE((SELECT jsonb_agg(snapshot ORDER BY player_id) "
            "FROM active_equipment_drains),'[]'::jsonb) AS drains"
        ).fetchone()
        targets = set()
        for snapshot in row["drains"]:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("outputs"), list):
                raise ValueError("invalid active equipment drain snapshot")
            # Future interruption scopes need an explicit target contract before
            # they may participate in admission; refuse rather than miss a fence.
            if snapshot.get("run_participants") or snapshot.get("actuators"):
                raise ValueError("unsupported active equipment drain admission scope")
            for output in snapshot["outputs"]:
                if not isinstance(output, dict):
                    raise ValueError("invalid active equipment drain output")
                frame_id = output.get("frame_id")
                if frame_id is None:
                    continue
                if not isinstance(frame_id, str) or not re.fullmatch(TARGET_ID_PATTERN, frame_id):
                    raise ValueError("invalid active equipment drain Frame")
                targets.add(f"frame:{frame_id}")
        policy = AdmissionPolicy(frozenset(targets))
        return (Runtime.restore(row["snapshot"], admission_policy=policy)
                if row["snapshot"] is not None else Runtime(policy))

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
        simulation. Operator routes must use this path so a concurrent drain
        cannot advance persisted Runtime past a time sampled before lock wait.
        """
        if method not in _TIMED_COMMANDS or "now" in kwargs:
            raise ValueError("unknown current-time Runtime command")
        with self.edit() as runtime:
            return getattr(runtime, method)(*args, self.clock.utc(), **kwargs)
