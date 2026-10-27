"""Transactional current Runtime ownership; never persist a future projection.

Every write names its actor (`central/actor.py`): `edit` stamps runtime_state's audit fields, and
each operator command (`command`, `command_current`) appends one runtime_changes row in the same
transaction (migration 111)."""

from contextlib import contextmanager

from psycopg.types.json import Jsonb

from central.actor import Actor
from central.db import Database
from central.infra.house_store import house_time_in
from central.runtime import Program, ProgramDraft, Runtime, Scene
from central.transaction_locks import RUNTIME_LOCK as RUNTIME_LOCK
from central.transaction_locks import acquire_runtime_locks
from contracts.time import Clock

_RUNTIME_COMMANDS = frozenset({
    "set_scene", "save_scene", "save_scene_and_apply_now", "delete_scene", "create_program", "save_program", "save_program_and_apply_now",
    "remove_program", "set_default_scene", "activate", "finish", "cancel", "advance",
})
_TIMED_COMMANDS = frozenset({
    "save_scene", "save_scene_and_apply_now", "create_program", "save_program", "save_program_and_apply_now", "remove_program",
    "set_default_scene", "activate", "finish", "cancel", "advance",
})
# Which id a command's leading string argument is.
_NAMED_BY_STRING = {
    "delete_scene": "scene_id", "set_default_scene": "scene_id", "activate": "scene_id",
    "remove_program": "program_id", "finish": "run_id", "cancel": "run_id",
}


def _change(method: str, args: tuple, result: object) -> dict:
    """The Scene, Run and Program a command names, from its arguments (and the Run it admitted)."""
    named: dict = {"scene_id": None, "run_id": None, "program_id": None, "scene_revision": None}
    for arg in args:
        if isinstance(arg, Scene):
            named.update(scene_id=arg.scene_id, scene_revision=arg.revision)
        elif isinstance(arg, Program):
            named.update(program_id=arg.program_id, scene_id=arg.scene_id)
        elif isinstance(arg, ProgramDraft):
            named["scene_id"] = arg.scene_id
    if args and isinstance(args[0], str) and method in _NAMED_BY_STRING:
        named[_NAMED_BY_STRING[method]] = args[0]
    run_id = getattr(result, "run_id", None)
    if isinstance(run_id, str) and named["run_id"] is None:
        named["run_id"] = run_id
    return named


class RuntimeStore:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    @contextmanager
    def edit(self, conn=None, *, by: Actor):
        if conn is None:
            with self.db.transaction() as transaction, self.edit(transaction, by=by) as runtime:
                yield runtime
            return
        acquire_runtime_locks(conn)
        runtime = self.read_in(conn)
        yield runtime
        conn.execute(
            "INSERT INTO runtime_state(singleton,revision,snapshot,created_at,created_by,"
            "changed_at,changed_by) VALUES(TRUE,1,%(snapshot)s,%(at)s,%(by)s,%(at)s,%(by)s) "
            "ON CONFLICT(singleton) DO UPDATE SET revision=runtime_state.revision+1,"
            "snapshot=EXCLUDED.snapshot,changed_at=EXCLUDED.changed_at,"
            "changed_by=EXCLUDED.changed_by",
            {"snapshot": Jsonb(runtime.export_state()), "at": self.clock.utc(), "by": by},
        )

    def record_change_in(self, conn, method: str, args: tuple, *, by: Actor,
                         result: object = None) -> None:
        """Append one operator Runtime command to runtime_changes, in the caller's transaction."""
        conn.execute(
            "INSERT INTO runtime_changes(at,by,command,scene_id,run_id,program_id,scene_revision) "
            "VALUES(to_timestamp(%(at)s),%(by)s,%(command)s,%(scene_id)s,%(run_id)s,"
            "%(program_id)s,%(scene_revision)s)",
            {"at": self.clock.utc(), "by": by, "command": method, **_change(method, args, result)},
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

    def command(self, method: str, *args, by: Actor, **kwargs):
        # The HTTP adapter cannot call arbitrary object methods through operator input.
        if method not in _RUNTIME_COMMANDS:
            raise ValueError("unknown Runtime command")
        with self.db.transaction() as conn, self.edit(conn, by=by) as runtime:
            result = getattr(runtime, method)(*args, **kwargs)
            self.record_change_in(conn, method, args, by=by, result=result)
            return result

    def command_current(self, method: str, *args, by: Actor, **kwargs):
        """Apply a wall-clock command at the post-lock serialization cut.

        Explicit-time ``command`` remains available for controllable-clock
        simulation. Operator routes must use this path so a concurrent writer
        cannot advance persisted Runtime past a time sampled before lock wait.
        """
        if method not in _TIMED_COMMANDS or "now" in kwargs:
            raise ValueError("unknown current-time Runtime command")
        with self.db.transaction() as conn, self.edit(conn, by=by) as runtime:
            result = getattr(runtime, method)(*args, self.clock.utc(), **kwargs)
            self.record_change_in(conn, method, args, by=by, result=result)
            return result
