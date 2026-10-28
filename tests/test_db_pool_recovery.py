"""Central's connection pool recovers by itself once the database is back (2026-10-28).

The database was unreachable for 4 h 46 min. psycopg_pool takes a timed-out waiter out of its
queue only when a connection is added or returned, and with a `max_waiting` cap it refuses a
request (`TooManyRequests`) before asking the pool to grow. So the cap's worth of timed-out
waiters stayed queued, the pool's growth attempt gave up after `reconnect_timeout`, and every
later request was refused without ever growing the pool: Central stayed down after the database
came back. Without a cap the queue would instead keep every timed-out request of the outage.
This test drives the real `Database` through such an outage behind a TCP proxy that is closed,
then opened: the queue stays within its cap while the database is away, and requests run again
once it is back.
"""

from __future__ import annotations

import socket
import threading
import time

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import TooManyRequests

from central.db import Database

POOL_SIZE = 2


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Proxy:
    """Forwards 127.0.0.1:`port` to the test database once started; before that nothing
    listens there, so a connection is refused, as during the outage."""

    def __init__(self, port: int, target: tuple[str, int]):
        self.port, self.target = port, target
        self.sockets: list[socket.socket] = []
        self.server: socket.socket | None = None

    def start(self) -> None:
        self.server = socket.create_server(("127.0.0.1", self.port))
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.target)
            self.sockets += [client, upstream]
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for end in (source, sink):
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        for sock in [self.server, *self.sockets]:
            if sock is not None:
                sock.close()


def test_the_pool_stays_bounded_during_an_outage_and_recovers_after_it(database):
    """Ten times `max_waiting` (2 x pool size) requests fail while the database is unreachable,
    yet the queue never holds more than `max_waiting`; the pool's growth attempt gives up, and
    then the database comes back: within one growth attempt a transaction runs again and no
    request is left waiting. Mutation probes in central/db.py: drop `max_waiting` (the queue
    holds every timed-out request of the outage), or drop the `check()` on `TooManyRequests`
    (every later request is refused and none grows the pool)."""
    live = conninfo_to_dict(database.dsn)
    port = _free_port()
    proxy = _Proxy(port, (live.get("host") or "localhost", int(live.get("port") or 5432)))
    db = Database(make_conninfo(database.dsn, host="127.0.0.1", port=str(port)),
                  pool_size=POOL_SIZE, reconnect_timeout=1.0)
    cap = POOL_SIZE * 2
    try:
        db.open()
        failures: list[BaseException] = []

        def attempt() -> None:
            try:
                with db.transaction() as conn:
                    conn.execute("SELECT 1")
            except Exception as exc:  # noqa: BLE001 - every attempt must fail while down
                failures.append(exc)

        down = [threading.Thread(target=attempt) for _ in range(cap * 10)]
        for thread in down:
            thread.start()
        for thread in down:
            thread.join()
        assert len(failures) == len(down)
        # No connection arrived, so every waiter that timed out is still queued.
        assert db.pool_stats()["requests_waiting"] <= cap
        time.sleep(3)   # past reconnect_timeout: the growth attempt has given up

        proxy.start()
        deadline = time.monotonic() + 10
        while True:
            try:
                with db.transaction() as conn:
                    assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
                break
            except TooManyRequests:
                # The refusal started the growth attempt; the queue drains once it connects.
                assert time.monotonic() < deadline, "the pool never grew after the outage"
                time.sleep(.05)
        assert db.pool_stats()["requests_waiting"] == 0
    finally:
        db.close()
        proxy.close()
