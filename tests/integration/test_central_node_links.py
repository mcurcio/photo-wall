"""Central's NodeLink supervisors record a Node's bus in PostgreSQL (E3d S1; E3b design §4 rules 1 and
3, §7.4, §9.1, §9.3, §9.5, §11 rows "Central crash mid-drain", "Rolling Central deploy", "Event buffer
full while Central is away", "Bus crash").

A real hub on Fleet's generated configuration, a real Node bus on the shipped configuration, and two
real Node sessions: `display` (fleet pipe: records, state, desired) and `content` (show pipe:
observations, state). Central's two supervisors (`NodeLinks`, one per pipe, over `PgLinkStores`)
track the Node's serial. 1: every event and state change is recorded once and in order, the
projection's document reaches the Node with Central's own token recorded, and each pipe's cursors
name only its own streams. 2: the links stop (records kept); the Node's event buffer drops its
oldest; the links come back and record one counted gap row and the rest. 3: a second worker's
supervisors overlap the first (a rolling deploy): every event is recorded once, nobody fails. 4:
the bus is killed (kill -9) and starts empty: one "unknown" gap row per stream that had a cursor,
then the new epoch's events, births and document.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from collections.abc import Sequence

from integration.bus_servers import (
    BusServer,
    Projection,
    hub_server,
    leaf_connections,
    line_slices,
    local,
    node_server,
    until,
)
from nats.js.errors import NotFoundError

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database
from central.infra.node_link_store import PgLinkStores
from central.infra.node_links import NodeLinks
from contracts.node_link import Pipe, account_id, central_user
from nodeapi.epoch import epoch_of
from nodeapi.node import NodeSession, Release

SERIAL = "serial-links"
DEVICE_ID = device_id_for_serial(SERIAL)
DISPLAY = Release("1.0.0", "sha256:display-release", {"display": 1})
CONTENT = Release("1.0.0", "sha256:content-release", {"content": 1})
RECORDS = "RECORD_display"
DISPLAY_STATE = "KV_state_display"
DESIRED = "KV_desired_display"
OBSERVATIONS = "OBSERVATION_content"
CONTENT_STATE = "KV_state_content"
FLEET_STREAMS = {RECORDS, DISPLAY_STATE}
SHOW_STREAMS = {OBSERVATIONS, CONTENT_STATE}
SECONDS = 30


class _Worker:
    """One Central worker's bus side: the fleet and show supervisors over one Database."""

    def __init__(self, db: Database, hub: BusServer, projection: Projection) -> None:
        stores = PgLinkStores(db)
        self.supervisors = [NodeLinks(pipe, hub.client_url, stores, projection) for pipe in (Pipe.FLEET, Pipe.SHOW)]
        self.stop = asyncio.Event()
        self.running = [asyncio.create_task(supervisor.run(self.stop)) for supervisor in self.supervisors]

    async def track(self, serials: Sequence[str]) -> None:
        await asyncio.gather(*(supervisor.track(serials) for supervisor in self.supervisors))

    async def close(self) -> None:
        """Stop both supervisors; raises what either raised."""
        self.stop.set()
        await asyncio.wait_for(asyncio.gather(*self.running), SECONDS)


async def _rows(db: Database, sql: str, params: Sequence[object] = ()) -> list[dict]:
    def run() -> list[dict]:
        with db.transaction() as conn:
            return conn.execute(sql, params).fetchall()
    return await asyncio.to_thread(run)


async def _records(db: Database, stream: str, epoch: str | None = None) -> list[dict]:
    """The stream's recorded rows, by (epoch, seq); one epoch's when named."""
    rows = await _rows(db, "SELECT epoch, seq, subject, message_id, data FROM node_link_records "
                           "WHERE device_id=%s AND stream=%s ORDER BY epoch, seq", (DEVICE_ID, stream))
    return [{**row, "data": bytes(row["data"])} for row in rows if epoch is None or row["epoch"] == epoch]


async def _gaps(db: Database) -> list[tuple]:
    rows = await _rows(db, "SELECT stream, epoch, after_seq, lost FROM node_link_gaps WHERE device_id=%s "
                           "ORDER BY stream, epoch, after_seq", (DEVICE_ID,))
    return [(row["stream"], row["epoch"], row["after_seq"], row["lost"]) for row in rows]


async def _cursors(db: Database) -> dict[str, dict[str, tuple[str, int]]]:
    """pipe -> stream -> (epoch, seq)."""
    rows = await _rows(db, "SELECT pipe, stream, epoch, seq FROM node_link_cursors WHERE device_id=%s", (DEVICE_ID,))
    cursors: dict[str, dict[str, tuple[str, int]]] = {}
    for row in rows:
        cursors.setdefault(row["pipe"], {})[row["stream"]] = (row["epoch"], row["seq"])
    return cursors


async def _born(db: Database, stream: str, epoch: str) -> bool:
    key = "$KV." + stream.removeprefix("KV_") + ".birth"
    return any(row["subject"] == key for row in await _records(db, stream, epoch))


async def _epoch(node_client, stream: str) -> str | None:
    try:
        return epoch_of(await node_client.jetstream().stream_info(stream))
    except NotFoundError:
        return None


async def _document_on_node(db: Database, node_client, value: bytes) -> str | None:
    """The desired bucket's epoch once the Node holds `value` for `show` and Central's own-token row
    names that very message."""
    try:
        message = await node_client.jetstream().get_last_msg(DESIRED, "$KV.desired_display.show")
    except NotFoundError:
        return None
    epoch = await _epoch(node_client, DESIRED)
    own = await _rows(db, "SELECT epoch, seq FROM node_link_documents WHERE device_id=%s AND stream=%s AND key=%s",
                      (DEVICE_ID, DESIRED, "show"))
    if message.data != value or not own or (own[0]["epoch"], own[0]["seq"]) != (epoch, message.seq):
        return None
    return epoch


def _central_connections(hub: BusServer) -> int:
    """Central's clients in the Node's hub account, from the hub's /connz."""
    with urllib.request.urlopen(f"{hub.monitor_url}/connz?auth=1&limit=1024", timeout=2) as response:
        connections = json.load(response).get("connections") or []
    return sum(1 for connection in connections if connection.get("authorized_user") == central_user(SERIAL))


def test_central_records_every_node_fact_once(database, tmp_path):
    with database.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (DEVICE_ID, SERIAL))
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    slices = line_slices()
    display = NodeSession("display", slices["display"], DISPLAY, url=node.client_url)
    content = NodeSession("content", slices["content"], CONTENT, url=node.client_url)
    projection = Projection()
    projection.streams[DESIRED] = {"show": b"show-1"}
    hub.start()
    node.start()

    async def linked() -> None:
        async def check():
            return account_id(SERIAL) in leaf_connections(hub)
        await until(check, 15, "the Node's leaf links")

    async def run() -> None:
        await linked()
        node_client = await local(node)
        worker = _Worker(database, hub, projection)
        second = second_db = None
        try:
            display.start()
            content.start()
            await worker.track([SERIAL])

            # 1. Steady, pipes apart.
            shown = [f"shown-{index:03}".encode() for index in range(50)]
            for body in shown:
                display.events.emit("record.shown", body, schema_major=1)
            display.state.put("state0", b"on")
            seen = [f"seen-{index:03}".encode() for index in range(20)]
            for body in seen:
                content.events.emit("observation.seen", body, schema_major=1)

            async def steady():
                epoch = await _epoch(node_client, RECORDS)
                state_epoch = await _epoch(node_client, DISPLAY_STATE)
                content_epoch = await _epoch(node_client, CONTENT_STATE)
                if None in (epoch, state_epoch, content_epoch):
                    return None
                records = await _records(database, RECORDS)
                state = await _records(database, DISPLAY_STATE)
                done = ([row["data"] for row in records] == shown
                        and [row["data"] for row in await _records(database, OBSERVATIONS)] == seen
                        and await _born(database, DISPLAY_STATE, state_epoch)
                        and await _born(database, CONTENT_STATE, content_epoch)
                        and any(row["subject"].endswith(".state0") and row["data"] == b"on" for row in state)
                        and await _document_on_node(database, node_client, b"show-1") is not None
                        and {pipe: set(streams) for pipe, streams in (await _cursors(database)).items()}
                        == {"fleet": FLEET_STREAMS, "show": SHOW_STREAMS})
                return epoch if done else None
            epoch = await until(steady, SECONDS, "both pipes record the display and content lines")
            desired_epoch = await _epoch(node_client, DESIRED)
            records = await _records(database, RECORDS)
            assert {row["epoch"] for row in records} == {epoch}
            assert [row["seq"] for row in records] == sorted(row["seq"] for row in records)
            ids = [row["message_id"] for row in records]
            assert None not in ids and len(set(ids)) == len(ids) == 50
            assert await _gaps(database) == []

            # 2. Away and overflow: the links stop and the records stay; the buffer drops its oldest.
            await worker.track([])
            total = "SELECT count(*) AS n FROM node_link_records WHERE device_id=%s"
            held = (await _rows(database, total, (DEVICE_ID,)))[0]["n"]
            last = max(row["seq"] for row in await _records(database, RECORDS, epoch))
            gaps = await _gaps(database)
            count = slices["display"].buffers[0].max_bytes // 1024 + 200
            away = [f"away-{index:05}".encode().ljust(1024, b".") for index in range(count)]
            for body in away:
                display.events.emit("record.shown", body, schema_major=1)

            async def published():
                state = (await node_client.jetstream().stream_info(RECORDS)).state
                return state if state.last_seq == last + count else None
            state = await until(published, SECONDS, "the session publishes every event")
            dropped = state.first_seq - last - 1
            assert dropped > 0 and state.messages == count - dropped
            assert (await _rows(database, total, (DEVICE_ID,)))[0]["n"] == held, "a stopped link recorded"

            await worker.track([SERIAL])

            async def caught_up():
                rows = await _records(database, RECORDS, epoch)
                return rows if rows[-1]["seq"] == state.last_seq else None
            rows = await until(caught_up, SECONDS, "the links come back and drain the rest")
            assert [gap for gap in await _gaps(database) if gap not in gaps] == [(RECORDS, epoch, last, dropped)]
            assert [row["data"] for row in rows if row["seq"] > last] == away[dropped:]
            assert [row["seq"] for row in rows if row["seq"] > last] == list(range(state.first_seq, state.last_seq + 1))
            ids = [row["message_id"] for row in rows]
            assert None not in ids and len(set(ids)) == len(ids)

            # 3. Two workers overlap (rolling deploy): each event is recorded once.
            second_db = Database(database.dsn)
            second = _Worker(second_db, hub, projection)
            await second.track([SERIAL])

            async def both_linked():
                return _central_connections(hub) == 4
            await until(both_linked, SECONDS, "both workers' fleet and show links connect")
            overlap = [f"overlap-{index:03}".encode() for index in range(100)]
            for body in overlap:
                display.events.emit("record.shown", body, schema_major=1)

            async def overlapped():
                rows = await _records(database, RECORDS, epoch)
                return rows if [row["data"] for row in rows[-100:]] == overlap else None
            rows = await until(overlapped, SECONDS, "the overlapping workers record the events")
            assert len({row["data"] for row in rows}) == len(rows)
            assert len({row["message_id"] for row in rows}) == len(rows)
            await second.track([])
            await second.close()
            assert not any(task.done() for task in worker.running)

            # 4. Bus kill -9 and restart: the store starts empty in new epochs.
            before = await _cursors(database)
            assert {pipe: set(streams) for pipe, streams in before.items()} == {"fleet": FLEET_STREAMS,
                                                                                "show": SHOW_STREAMS}
            ended = {(stream, cursor[0]) for streams in before.values() for stream, cursor in streams.items()}
            await node_client.close()
            node.crash()
            node.start()
            await linked()
            node_client = await local(node)
            after = [f"after-{index:03}".encode() for index in range(10)]
            for body in after:
                display.events.emit("record.shown", body, schema_major=1)

            async def restarted():
                epochs = {stream: await _epoch(node_client, stream)
                          for stream in (RECORDS, DISPLAY_STATE, CONTENT_STATE)}
                if None in epochs.values() or epochs[RECORDS] == epoch:
                    return None
                done = ([row["data"] for row in await _records(database, RECORDS, epochs[RECORDS])] == after
                        and await _born(database, DISPLAY_STATE, epochs[DISPLAY_STATE])
                        and await _born(database, CONTENT_STATE, epochs[CONTENT_STATE])
                        and {(gap[0], gap[1]) for gap in await _gaps(database) if gap[3] is None} == ended)
                desired = await _document_on_node(database, node_client, b"show-1")
                return epochs if done and desired is not None and desired != desired_epoch else None
            await until(restarted, SECONDS, "the links record the new epochs and the document is back")
            unknown = [gap for gap in await _gaps(database) if gap[3] is None]
            assert sorted((gap[0], gap[1]) for gap in unknown) == sorted(ended)
            assert {stream: cursor[1] for streams in before.values() for stream, cursor in streams.items()} == {
                stream: after_seq for stream, _, after_seq, _ in unknown}
            assert not any(task.done() for task in worker.running)
        finally:
            for session in (display, content):
                await asyncio.to_thread(session.stop)
            if second is not None and not second.stop.is_set():
                await second.close()
            await worker.close()
            await node_client.close()
            if second_db is not None:
                second_db.close()

    try:
        asyncio.run(run())
    finally:
        display.stop()
        content.stop()
        node.stop()
        hub.stop()
