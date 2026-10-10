"""What Central read from each Node's bus, in PostgreSQL: `nodeapi.hub.LinkStore` (E3b design §7.4,
§9.3; migration 066).

One `PgLinkStore` per (Node, pipe) link. A drained batch is one transaction: its gap rows, its raw
records keyed (Node, stream, epoch, sequence), then the stream's cursor, so a crash mid-drain resumes
from the cursor and a repeat is a no-op (`ON CONFLICT DO NOTHING`). Two workers that overlap in a
rolling deploy write the same keys; the cursor is last write wins. Central records before it judges:
nothing here interprets a record. Times are PostgreSQL's (`DatabaseTransactionClock`), never a Node's.

Every call runs its SQL in a worker thread behind one gate per process (`PgLinkStores`), so at most
LINK_STORE_CONCURRENCY store calls hold or wait for a pooled connection: the rest wait in the event
loop, never in the pool's bounded queue, whose overflow would end the worker in normal operation
(erratum E-E3D-CUT-9).

`PgWallMarks` is `nodeapi.hub.WallMarks` over migration 067: the highest WALL sequence Central
recorded, which only ever rises, so WALL re-created after an empty hub start begins past every Node
mirror (mark + 1 + WALL_MARGIN); Central's wall documents come from the injected source (E5/E8).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol, TypeVar

from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database, DatabaseTransactionClock
from contracts.node_link import Pipe
from nodeapi.envelope import message_id
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Gap, Read

LINK_STORE_CONCURRENCY: Final = 4   # store calls in flight at once across every link of a process; the
                                    # rest wait in the event loop, never in the pool's queue (E-E3D-CUT-9)

_T = TypeVar("_T")
_CLOCK: Final = DatabaseTransactionClock()


class PgLinkStores:
    """One per process: every (Node, pipe) link's store over one Database, behind one gate."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._gate = asyncio.Semaphore(LINK_STORE_CONCURRENCY)

    def store(self, serial: str, pipe: Pipe) -> PgLinkStore:
        device_id = device_id_for_serial(serial)
        if device_id is None:
            raise ValueError("node_link_serial")
        return PgLinkStore(self, device_id, Pipe(pipe))

    async def _run(self, body: Callable[[Any], _T]) -> _T:
        """`body(conn)` in one transaction, in a worker thread, behind the gate."""
        def run() -> _T:
            with self._db.transaction() as conn:
                return body(conn)
        async with self._gate:
            return await asyncio.to_thread(run)


class PgLinkStore:
    """nodeapi.hub.LinkStore for one Node and one pipe. Every method runs its SQL in a worker thread
    (asyncio.to_thread) under the PgLinkStores gate; times come from DatabaseTransactionClock."""

    def __init__(self, stores: PgLinkStores, device_id: str, pipe: Pipe) -> None:
        self._stores = stores
        self._device_id = device_id
        self._pipe = pipe

    async def cursors(self) -> Mapping[str, Token]:
        """This Node's cursors of THIS PIPE only: NodeLink.reconcile ends every cursor whose stream it
        does not see, so another pipe's cursors must never reach it."""
        def body(conn) -> Mapping[str, Token]:
            rows = conn.execute("SELECT stream, epoch, seq FROM node_link_cursors WHERE device_id=%s AND pipe=%s",
                                (self._device_id, self._pipe.value)).fetchall()
            return {row["stream"]: Token(row["epoch"], row["seq"]) for row in rows}
        return await self._stores._run(body)

    async def commit(self, stream: str, batch: Batch) -> None:
        """One transaction: gap rows, then raw records, both idempotent; then the cursor, upserted
        with this pipe, or removed when the batch ends the stream (cursor None)."""
        def body(conn) -> None:
            now = _CLOCK.now_in(conn)
            gaps = [(self._device_id, stream, item.epoch, item.after, item.count, now)
                    for item in batch.items if isinstance(item, Gap)]
            records = [(self._device_id, stream, item.token.epoch, item.token.seq, item.subject,
                        message_id(item.headers), Jsonb(dict(item.headers or {})), item.data, now)
                       for item in batch.items if isinstance(item, Read)]
            with conn.cursor() as cursor:
                if gaps:
                    cursor.executemany(
                        "INSERT INTO node_link_gaps(device_id, stream, epoch, after_seq, lost, recorded_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", gaps)
                if records:
                    cursor.executemany(
                        "INSERT INTO node_link_records(device_id, stream, epoch, seq, subject, message_id, headers, "
                        "data, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        records)
            if batch.cursor is None:
                conn.execute("DELETE FROM node_link_cursors WHERE device_id=%s AND stream=%s",
                             (self._device_id, stream))
            else:
                conn.execute(
                    "INSERT INTO node_link_cursors(device_id, stream, pipe, epoch, seq) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (device_id, stream) DO UPDATE "
                    "SET pipe=EXCLUDED.pipe, epoch=EXCLUDED.epoch, seq=EXCLUDED.seq",
                    (self._device_id, stream, self._pipe.value, batch.cursor.epoch, batch.cursor.seq))
        await self._stores._run(body)

    async def action(self, kind: str, body: Mapping[str, object]) -> None:
        def write(conn) -> None:
            conn.execute("INSERT INTO node_link_actions(device_id, pipe, kind, body, recorded_at) "
                         "VALUES (%s, %s, %s, %s, %s)",
                         (self._device_id, self._pipe.value, kind, Jsonb(dict(body)), _CLOCK.now_in(conn)))
        await self._stores._run(write)

    async def own_tokens(self, stream: str) -> Mapping[str, tuple[str, Token]]:
        def body(conn) -> Mapping[str, tuple[str, Token]]:
            rows = conn.execute("SELECT key, digest, epoch, seq FROM node_link_documents "
                                "WHERE device_id=%s AND stream=%s", (self._device_id, stream)).fetchall()
            return {row["key"]: (row["digest"], Token(row["epoch"], row["seq"])) for row in rows}
        return await self._stores._run(body)

    async def wrote(self, stream: str, key: str, digest: str, token: Token) -> None:
        def body(conn) -> None:
            conn.execute(
                "INSERT INTO node_link_documents(device_id, stream, key, digest, epoch, seq) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (device_id, stream, key) DO UPDATE "
                "SET digest=EXCLUDED.digest, epoch=EXCLUDED.epoch, seq=EXCLUDED.seq",
                (self._device_id, stream, key, digest, token.epoch, token.seq))
        await self._stores._run(body)


class WallDocuments(Protocol):
    async def wall_documents(self) -> Mapping[str, bytes]: ...   # Central's wall documents (E5/E8); none until then


class PgWallMarks:
    """nodeapi.hub.WallMarks over node_bus_wall; wall documents from the injected source."""

    def __init__(self, db: Database, documents: WallDocuments) -> None:
        self._db = db
        self._documents = documents

    async def mark(self) -> int:
        """The highest WALL sequence recorded; 0 with no row."""
        def body() -> int:
            with self._db.transaction() as conn:
                row = conn.execute("SELECT mark FROM node_bus_wall").fetchone()
            return 0 if row is None else int(row["mark"])
        return await asyncio.to_thread(body)

    async def record_mark(self, seq: int) -> None:
        """Upsert GREATEST(mark, seq): a late or repeated record never lowers it."""
        def body() -> None:
            with self._db.transaction() as conn:
                conn.execute("INSERT INTO node_bus_wall(mark) VALUES (%s) ON CONFLICT (singleton) DO UPDATE "
                             "SET mark=GREATEST(node_bus_wall.mark, EXCLUDED.mark)", (seq,))
        await asyncio.to_thread(body)

    async def wall_documents(self) -> Mapping[str, bytes]:
        return await self._documents.wall_documents()
