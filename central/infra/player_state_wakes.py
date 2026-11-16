"""When a Player's state may have changed, so Central's session socket pushes it then and only then
(2026-11-16: the socket re-read and re-sent an unchanged state every half second, each a delivery
under the fleet-wide Coordination lock). Event-driven, never a timer.

The event: a transaction that holds the Coordination lock and wrote (`notify_if_written_in`, the
last statement of the Coordinator's and the Registry's writing transactions). Everything a
Player's state is projected from (its configuration, plan offers, grants, revocations, the
Identify cue, its enrollment) is written under that lock, so its commit is the one moment a state
can change. A transaction that wrote nothing (a state poll of an unchanged state) notifies nothing,
so a delivery that wakes a socket never wakes it again. PostgreSQL sends the NOTIFY only when the
transaction commits; one rolled back sends none. Every Central process hears it on one LISTEN
(`PlayerStateWakes.listen`), whichever process wrote.

Each woken socket reads its Player's state again and sends it only when its delivery changed;
a Player's state poll still reads it every REPORT_INTERVAL, so a missed wake costs at most one poll.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any, Final

from central.infra.listen import listen

PLAYER_STATE_CHANNEL: Final = "photo_wall_player_state"     # payload: empty (every Player)
_LISTEN_RETRY_SECONDS: Final = 1.0
_LISTEN_APPLICATION: Final = "photo-wall-player-state"


def notify_if_written_in(conn: Any) -> None:
    """Wake every Central's Player state sockets when the caller's transaction commits, if it
    wrote anything (it holds a transaction id); nothing for a transaction that only read."""
    conn.execute("SELECT pg_notify(%s, '') WHERE pg_current_xact_id_if_assigned() IS NOT NULL",
                 (PLAYER_STATE_CHANNEL,))


class PlayerStateWakes:
    """A count of wakes, so a waiter never misses one that came between two waits: `wake` from
    any thread, `after` on the event loop, `listen` for every process's commits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    def count(self) -> int:
        with self._lock:
            return self._count

    def wake(self, _payload: str = "") -> None:
        with self._lock:
            self._count += 1
            waiters, self._waiters = self._waiters, []
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there
                loop.call_soon_threadsafe(event.set)

    async def after(self, seen: int) -> int:
        """The count, once it differs from `seen`."""
        loop = asyncio.get_running_loop()
        while True:
            event = asyncio.Event()
            with self._lock:
                if self._count != seen:
                    return self._count
                self._waiters.append((loop, event))
            await event.wait()

    async def listen(self, dsn: str) -> None:
        """Until cancelled: LISTEN on one connection of its own, waking on each NOTIFY and on every
        (re)connect (notifications sent while none listened are lost)."""
        await listen(dsn, PLAYER_STATE_CHANNEL, application=_LISTEN_APPLICATION,
                     connected=self.wake, notified=self.wake, retry_seconds=_LISTEN_RETRY_SECONDS)


__all__ = ["PLAYER_STATE_CHANNEL", "PlayerStateWakes", "notify_if_written_in"]
