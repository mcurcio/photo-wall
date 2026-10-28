"""Transactional current Runtime ownership; never persist a future projection.

Every write names its actor (`central/actor.py`) and its command, and `edit`, the one writer of
runtime_state, audits it (migration 111): it stamps runtime_state's audit fields, and appends one
runtime_changes row for each Scene, Program, Run and Default Scene the command changed, found by
comparing the state before and after it (`_changes`). No caller writes the audit, so none can
skip it."""

from contextlib import contextmanager

from psycopg.types.json import Jsonb

from central.actor import Actor
from central.db import Database
from central.infra.house_store import house_time_in
from central.runtime import Runtime
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
_NO_IDS = {"scene_id": None, "run_id": None, "program_id": None, "scene_revision": None}


def _changes(before: dict, after: dict) -> list[dict]:
    """What one write changed, as runtime_changes rows: each Scene, Program and Default Scene
    whose stored form differs, and each Run admitted, moved to another phase or removed (a Run's
    turns moving on is not a change). A command that changed only admissions or the queue is one
    row naming none of them."""
    rows: list[dict] = []
    for scene_id in sorted(before["scenes"].keys() | after["scenes"].keys()):
        old, new = before["scenes"].get(scene_id), after["scenes"].get(scene_id)
        if old != new:
            rows.append({**_NO_IDS, "scene_id": scene_id,
                         "scene_revision": (new or old)["revision"]})
    for program_id in sorted(before["programs"].keys() | after["programs"].keys()):
        old, new = before["programs"].get(program_id), after["programs"].get(program_id)
        if old != new:
            rows.append({**_NO_IDS, "program_id": program_id,
                         "scene_id": (new or old)["scene_id"]})
    if before.get("default_scene") != after.get("default_scene"):
        chosen = after.get("default_scene") or before.get("default_scene")
        rows.append({**_NO_IDS, "scene_id": chosen["scene_id"]})
    for run_id in sorted(before["runs"].keys() | after["runs"].keys()):
        old, new = before["runs"].get(run_id), after["runs"].get(run_id)
        if old is None or new is None or old["phase"] != new["phase"]:
            scene = (new or old)["scene"]
            rows.append({**_NO_IDS, "run_id": run_id, "scene_id": scene["scene_id"],
                         "scene_revision": scene["revision"]})
    if not rows and any(before.get(key) != after.get(key) for key in ("admissions", "queue")):
        rows.append(dict(_NO_IDS))
    return rows


class RuntimeStore:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    @contextmanager
    def edit(self, conn=None, *, by: Actor, command: str):
        """The one writer of runtime_state: the Runtime as stored, saved on exit with its audit
        fields, and one runtime_changes row per thing `command` changed (`_changes`), all in the
        caller's transaction."""
        if conn is None:
            with self.db.transaction() as transaction, self.edit(
                    transaction, by=by, command=command) as runtime:
                yield runtime
            return
        acquire_runtime_locks(conn)
        runtime = self.read_in(conn)
        before = runtime.export_state()
        yield runtime
        after = runtime.export_state()
        at = self.clock.utc()
        conn.execute(
            "INSERT INTO runtime_state(singleton,revision,snapshot,created_at,created_by,"
            "changed_at,changed_by) VALUES(TRUE,1,%(snapshot)s,%(at)s,%(by)s,%(at)s,%(by)s) "
            "ON CONFLICT(singleton) DO UPDATE SET revision=runtime_state.revision+1,"
            "snapshot=EXCLUDED.snapshot,changed_at=EXCLUDED.changed_at,"
            "changed_by=EXCLUDED.changed_by",
            {"snapshot": Jsonb(after), "at": at, "by": by},
        )
        for change in _changes(before, after):
            conn.execute(
                "INSERT INTO runtime_changes(at,by,command,scene_id,run_id,program_id,"
                "scene_revision) VALUES(to_timestamp(%(at)s),%(by)s,%(command)s,%(scene_id)s,"
                "%(run_id)s,%(program_id)s,%(scene_revision)s)",
                {"at": at, "by": by, "command": command, **change})

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
        with self.edit(by=by, command=method) as runtime:
            return getattr(runtime, method)(*args, **kwargs)

    def command_current(self, method: str, *args, by: Actor, **kwargs):
        """Apply a wall-clock command at the post-lock serialization cut.

        Explicit-time ``command`` remains available for controllable-clock
        simulation. Operator routes must use this path so a concurrent writer
        cannot advance persisted Runtime past a time sampled before lock wait.
        """
        if method not in _TIMED_COMMANDS or "now" in kwargs:
            raise ValueError("unknown current-time Runtime command")
        with self.edit(by=by, command=method) as runtime:
            return getattr(runtime, method)(*args, self.clock.utc(), **kwargs)
