"""Transactional current Runtime ownership; never persist a future projection.

Every write names its actor (`central/actor.py`) and its command, and `edit`, the one writer of
runtime_state, stamps runtime_state's audit fields (`created_by`, `changed_by` and their times) with
that actor. No caller writes the audit fields, so none can skip them.

The row is written only when the Runtime changed apart from its processed instant `now` (decision
0034, class A): a tick that only moved time writes nothing, takes no transaction id and so wakes
no Player. The stored `now` is therefore the time of the last change, possibly hours old; the one
reader that depends on its freshness, `Runtime._pending_windows`, is kept right by Settings ›
House, which saves the processed instant (`save_processed_in`) before it moves the house's time.

A save wakes the scheduler and every Player socket when it commits (`notify_if_written_in`,
central/infra/player_state_wakes.py), whoever wrote it: an operator's Scene, Program or activation
is planned at once, not at the scheduler's next rest (central/scheduler.py)."""

from contextlib import contextmanager

from psycopg.types.json import Jsonb

from central.actor import Actor
from central.db import Database
from central.infra.house_store import house_time_in
from central.infra.player_state_wakes import notify_if_written_in
from central.runtime import Runtime
from central.transaction_locks import RUNTIME_LOCK as RUNTIME_LOCK
from central.transaction_locks import acquire_runtime_locks
from contracts.time import Clock

_RUNTIME_COMMANDS = frozenset({
    "set_scene", "save_scene", "save_scene_and_apply_now", "delete_scene", "create_program", "save_program", "save_program_and_apply_now",
    "remove_program", "set_default_scene", "activate", "finish", "cancel", "end", "advance",
})
_TIMED_COMMANDS = frozenset({
    "save_scene", "save_scene_and_apply_now", "create_program", "save_program", "save_program_and_apply_now", "remove_program",
    "set_default_scene", "activate", "finish", "cancel", "end", "advance",
})


def _apart_from_now(state: dict) -> dict:
    """`state` (an `export_state()`) without its `now`: what a save compares."""
    return {key: value for key, value in state.items() if key != "now"}


class RuntimeStore:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    @contextmanager
    def edit(self, conn=None, *, by: Actor, command: str):
        """The one writer of runtime_state: the Runtime as stored, saved on exit in the caller's
        transaction with its audit fields stamped by `by`. `command` names the write; nothing
        stores it (decision 0034 dropped the per-change log). The row is written only when the state after differs from the state
        before apart from `now` (`_apart_from_now`); then the whole state, `now` included, is
        saved. An edit that moved only `now` writes nothing, takes no transaction id, and so fires
        no Player wake (central/infra/player_state_wakes.py)."""
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
        if _apart_from_now(after) != _apart_from_now(before):
            self._save_in(conn, after, by=by)

    def save_processed_in(self, conn, *, by: Actor) -> None:
        """Advance the stored Runtime to Central's clock (`self.clock.utc()`, read after the
        locks) and save it with `now` even when nothing else changed: the one write of the
        processed instant alone, in the caller's transaction (takes the Coordination and Runtime
        locks, `acquire_runtime_locks`). Settings › House calls it before it stores a new zone or
        place, under the house still stored, so `Runtime._pending_windows` never admits a window
        the change moved before the processed instant (R10)."""
        acquire_runtime_locks(conn)
        runtime = self.read_in(conn)
        runtime.advance(self.clock.utc())
        self._save_in(conn, runtime.export_state(), by=by)

    def _save_in(self, conn, after: dict, *, by: Actor) -> None:
        at = self.clock.utc()
        conn.execute(
            "INSERT INTO runtime_state(singleton,revision,snapshot,created_at,created_by,"
            "changed_at,changed_by) VALUES(TRUE,1,%(snapshot)s,%(at)s,%(by)s,%(at)s,%(by)s) "
            "ON CONFLICT(singleton) DO UPDATE SET revision=runtime_state.revision+1,"
            "snapshot=EXCLUDED.snapshot,changed_at=EXCLUDED.changed_at,"
            "changed_by=EXCLUDED.changed_by",
            {"snapshot": Jsonb(after), "at": at, "by": by},
        )
        notify_if_written_in(conn)

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
