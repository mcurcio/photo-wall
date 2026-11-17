"""What Central read from each Node's bus, in PostgreSQL: `nodeapi.hub.LinkStore` (E3b design §7.4,
§9.3; decision 0034, migration 119).

One `PgLinkStore` per (Node, pipe) link. A drained batch is one transaction: its gaps and event
records into `events` (central/infra/events.py, keyed (Node, stream, epoch, sequence), so a repeat
is a no-op), each state value into `node_states` (the newest per state key, replaced only by a
different epoch or a higher sequence), then the stream's cursor inside the Node's `node_links` row,
so a crash mid-drain resumes from the cursor. Two workers that overlap in a rolling deploy write the
same keys; the cursor is last write wins. Central records before it judges: nothing here interprets
a record, and a stream's `RecordJudge` (injected) reads only the Reads its commit stored, in that
commit. Times are PostgreSQL's (`DatabaseTransactionClock`), never a Node's.

Every call runs its SQL in a worker thread behind one gate per process (`PgLinkStores`), so at most
LINK_STORE_CONCURRENCY store calls hold or wait for a pooled connection: the rest wait in the event
loop, never in the pool's bounded queue, whose overflow would end the worker in normal operation
(erratum E-E3D-CUT-9).

`PgWallMarks` is `nodeapi.hub.WallMarks` over the `singletons` document 'wall_mark': the highest WALL
sequence Central recorded, which only ever rises, so WALL re-created after an empty hub start begins
past every Node mirror (mark + 1 + WALL_MARGIN). It hands WallWriter no document: each wall key has
one writer of its own (the room-lights document's is the worker's `RoomLightsWall`), which
`wall_documents` wakes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeVar

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database, DatabaseTransactionClock
from central.infra.events import (
    GapEvent,
    RecordEvent,
    gap_events_in,
    link_action_in,
    record_events_in,
)
from central.infra.singletons import Singleton
from contracts.node_link import Pipe
from nodeapi.buffers import KV_OPERATION_HEADER
from nodeapi.envelope import message_id
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Gap, Read

LINK_STORE_CONCURRENCY: Final = 4   # store calls in flight at once across every link of a process; the
                                    # rest wait in the event loop, never in the pool's queue (E-E3D-CUT-9)

_T = TypeVar("_T")
_CLOCK: Final = DatabaseTransactionClock()
_KV_STREAM_PREFIX: Final = "KV_"   # a KV bucket's stream: its records are state, the newest per key kept


class RecordJudge(Protocol):
    """Reads one stream's newly recorded items inside the commit that records them (roadmap 1b,
    slice C1): Central records, then judges, in one transaction, so a judged fact never runs ahead
    of its raw record and a repeat (an event already logged, or a state no newer than the stored
    one) is judged at most once. `PgLinkStores`
    takes a judge per stream name (`KV_state_display` -> the Output report judge,
    central/infra/display_store.py); a stream with no judge is only recorded."""

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None: ...
        # `items` are the batch's Reads that this commit stored (not the repeats), in sequence order


class PgLinkStores:
    """One per process: every (Node, pipe) link's store over one Database, behind one gate, with a
    judge per stream name (`RecordJudge`)."""

    def __init__(self, db: Database, judges: Mapping[str, RecordJudge] = MappingProxyType({})) -> None:
        self._db = db
        self._judges = MappingProxyType(dict(judges))
        self._gate = asyncio.Semaphore(LINK_STORE_CONCURRENCY)

    def store(self, serial: str, pipe: Pipe) -> PgLinkStore:
        device_id = device_id_for_serial(serial)
        if device_id is None:
            raise ValueError("node_link_serial")
        return PgLinkStore(self, device_id, Pipe(pipe))

    def judge(self, stream: str) -> RecordJudge | None:
        return self._judges.get(stream)

    async def run(self, body: Callable[[Any], _T]) -> _T:
        """`body(conn)` in one transaction, in a worker thread, behind the gate: every store call,
        and every read of Central's document sources that shares this process's pool."""
        def run() -> _T:
            with self._db.transaction() as conn:
                return body(conn)
        async with self._gate:
            return await asyncio.to_thread(run)


class PgLinkStore:
    """nodeapi.hub.LinkStore for one Node and one pipe. Every method runs its SQL in a worker thread
    (asyncio.to_thread) under the PgLinkStores gate; times come from DatabaseTransactionClock. The
    Node's `node_links` row is created by its first cursor or document write (audit by "central")."""

    def __init__(self, stores: PgLinkStores, device_id: str, pipe: Pipe) -> None:
        self._stores = stores
        self._device_id = device_id
        self._pipe = pipe

    async def cursors(self) -> Mapping[str, Token]:
        """This Node's cursors of THIS PIPE only: NodeLink.reconcile ends every cursor whose stream it
        does not see, so another pipe's cursors must never reach it."""
        def body(conn) -> Mapping[str, Token]:
            rows = conn.execute(
                "SELECT c.key AS stream, c.value->>'epoch' AS epoch, (c.value->>'seq')::bigint AS seq"
                " FROM node_links l, jsonb_each(l.cursors) c"
                " WHERE l.device_id=%s AND c.value->>'pipe'=%s",
                (self._device_id, self._pipe.value)).fetchall()
            return {row["stream"]: Token(row["epoch"], int(row["seq"])) for row in rows}
        return await self._stores.run(body)

    async def commit(self, stream: str, batch: Batch) -> None:
        """One transaction, in this order: gaps -> `gap_events_in`; Reads of a 'KV_' stream ->
        `node_states` upsert, a row replaced only by a different epoch or a higher seq, the batch in
        sequence order; Reads of any other stream -> `record_events_in`. The judge sees exactly the
        Reads this commit stored (a state that replaced its key, or an event inserted), never a
        repeat; then the cursor, set or removed inside node_links.cursors with one statement (no
        read-modify-write)."""
        judge = self._stores.judge(stream)
        reads = [item for item in batch.items if isinstance(item, Read)]   # in sequence order

        def body(conn) -> None:
            now = _CLOCK.now_in(conn)
            gap_events_in(conn, [GapEvent(self._device_id, stream, item.epoch, item.after, item.count, now)
                                 for item in batch.items if isinstance(item, Gap)])
            if stream.startswith(_KV_STREAM_PREFIX):
                stored = self._states_in(conn, stream, reads, now)
            else:
                stored = record_events_in(conn, [
                    RecordEvent(self._device_id, stream, read.token.epoch, read.token.seq, read.subject,
                                message_id(read.headers), dict(read.headers or {}), read.data, now)
                    for read in reads])
            if judge is not None and stored:
                judge.judge_in(conn, self._device_id, stream, tuple(
                    read for read in reads if (read.token.epoch, read.token.seq) in stored))
            if batch.cursor is None:
                conn.execute(
                    "UPDATE node_links SET cursors = cursors - %(stream)s, changed_at=%(at)s,"
                    " changed_by='central' WHERE device_id=%(device)s AND cursors ? %(stream)s",
                    {"stream": stream, "at": now, "device": self._device_id})
            else:
                cursor = Jsonb({"pipe": self._pipe.value, "epoch": batch.cursor.epoch,
                                "seq": batch.cursor.seq})
                conn.execute(
                    "INSERT INTO node_links (device_id, cursors, created_at, created_by, changed_at,"
                    " changed_by) VALUES (%(device)s, jsonb_build_object(%(stream)s::text, %(cursor)s::jsonb),"
                    " %(at)s, 'central', %(at)s, 'central') ON CONFLICT (device_id) DO UPDATE"
                    " SET cursors = jsonb_set(node_links.cursors, ARRAY[%(stream)s::text], %(cursor)s::jsonb),"
                    " changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by"
                    " WHERE node_links.cursors -> %(stream)s::text IS DISTINCT FROM %(cursor)s::jsonb",
                    {"device": self._device_id, "stream": stream, "cursor": cursor, "at": now})
        await self._stores.run(body)

    def _states_in(self, conn: Any, stream: str, reads: list[Read], now: float) -> frozenset[tuple[str, int]]:
        """Each Read upserted as its key's newest value, in sequence order; answers the (epoch, seq)
        of the Reads stored (inserted, or replacing an older value of their key)."""
        if not reads:
            return frozenset()
        stored: set[tuple[str, int]] = set()
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO node_states (device_id, stream, subject, epoch, seq, removed, headers, data,"
                " recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (device_id, stream, subject) DO UPDATE SET epoch=EXCLUDED.epoch,"
                " seq=EXCLUDED.seq, removed=EXCLUDED.removed, headers=EXCLUDED.headers,"
                " data=EXCLUDED.data, recorded_at=EXCLUDED.recorded_at"
                " WHERE node_states.epoch <> EXCLUDED.epoch OR node_states.seq < EXCLUDED.seq"
                " RETURNING epoch, seq",
                [(self._device_id, stream, read.subject, read.token.epoch, read.token.seq,
                  KV_OPERATION_HEADER in (read.headers or {}), Jsonb(dict(read.headers or {})),
                  read.data, now) for read in reads], returning=True)
            while True:
                stored.update((row["epoch"], row["seq"]) for row in cursor.fetchall())
                if not cursor.nextset():
                    break
        return frozenset(stored)

    async def action(self, kind: str, body: Mapping[str, object]) -> None:
        """nodeapi's action log: one `events` row of kind 'link-action'."""
        await self._stores.run(lambda conn: link_action_in(conn, self._device_id, self._pipe, kind, body))

    async def own_tokens(self, stream: str) -> Mapping[str, tuple[str, Token]]:
        def body(conn) -> Mapping[str, tuple[str, Token]]:
            rows = conn.execute(
                "SELECT d.key, d.value->>'digest' AS digest, d.value->>'epoch' AS epoch,"
                " (d.value->>'seq')::bigint AS seq FROM node_links l,"
                " jsonb_each(coalesce(l.documents -> %s::text, '{}'::jsonb)) d WHERE l.device_id=%s",
                (stream, self._device_id)).fetchall()
            return {row["key"]: (row["digest"], Token(row["epoch"], int(row["seq"]))) for row in rows}
        return await self._stores.run(body)

    async def wrote(self, stream: str, key: str, digest: str, token: Token) -> None:
        """Central's own write of `key`: set inside node_links.documents with one statement."""
        def body(conn) -> None:
            value = Jsonb({"digest": digest, "epoch": token.epoch, "seq": token.seq})
            conn.execute(
                "INSERT INTO node_links (device_id, documents, created_at, created_by, changed_at,"
                " changed_by) VALUES (%(device)s, jsonb_build_object(%(stream)s::text,"
                " jsonb_build_object(%(key)s::text, %(value)s::jsonb)), %(at)s, 'central', %(at)s, 'central')"
                " ON CONFLICT (device_id) DO UPDATE SET documents = jsonb_set(node_links.documents,"
                " ARRAY[%(stream)s::text], coalesce(node_links.documents -> %(stream)s::text, '{}'::jsonb)"
                " || jsonb_build_object(%(key)s::text, %(value)s::jsonb)),"
                " changed_at=EXCLUDED.changed_at, changed_by=EXCLUDED.changed_by",
                {"device": self._device_id, "stream": stream, "key": key, "value": value,
                 "at": _CLOCK.now_in(conn)})
        await self._stores.run(body)


class WallMark(BaseModel):
    """`singletons` 'wall_mark': the highest WALL sequence Central recorded."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    mark: int = Field(ge=0)


WALL_MARK: Final[Singleton[WallMark]] = Singleton("wall_mark", WallMark)


class PgWallMarks:
    """nodeapi.hub.WallMarks over `singletons` 'wall_mark'; `wake` is called whenever WALL is
    ensured, so each wall key's own writer puts its document again."""

    def __init__(self, db: Database, wake: Callable[[], None]) -> None:
        self._db = db
        self._wake = wake

    async def mark(self) -> int:
        """The highest WALL sequence recorded; 0 with none."""
        def body() -> int:
            with self._db.transaction() as conn:
                stored = WALL_MARK.read_in(conn)
            return 0 if stored is None else stored.value.mark
        return await asyncio.to_thread(body)

    async def record_mark(self, seq: int) -> None:
        """Write `seq` only when it is higher than the stored mark: a late or repeated record never
        lowers it, and an equal one writes nothing."""
        def body() -> None:
            with self._db.transaction() as conn:
                stored = WALL_MARK.read_in(conn)
                if stored is None or seq > stored.value.mark:
                    WALL_MARK.write_in(conn, WallMark(mark=seq), by="central", times=_CLOCK)
        await asyncio.to_thread(body)

    async def wall_documents(self) -> Mapping[str, bytes]:
        """None: every wall key's writer puts its own document; this only wakes them."""
        self._wake()
        return {}
