"""Recovery on real servers: a Node session whose line another component declares (E3b design §7.2
node, §7.3, §9.5, §9.7, §11 attach row, §16.1 "also in E3b"; erratum E-E3B-R2-4), and every loss
reaching Central as one counted row, rollout skew and key-table changes (§9.2, §9.3, §11).

The Player's store line is declared by apps, from the held Player releases' slices; the Player
session never applies it. Attach before declare: the Player starts first and its events wait in the
outbox; once apps applies the line, the Player attaches and Central's show NodeLink records its
`birth` and every event in that epoch, and again in the next epoch after a bus crash with the start
order reversed. Hot-swap: apps applies the union of the old and new releases' slices, both Player
sessions attach, then apps applies the new slice alone; the old-only stream is pruned and Central
records an unknown gap for it. A release whose union with the running one does not fit the line is
refused before anything is applied. No session meets an absent stream on the way. Central's link
runs from before any line exists and finds each by the bus's stream names (erratum E-E3B-CC-1).

Losses: an event buffer that overflows while Central's link is away is one gap row with the exact
count, then every held event once; state written faster than the link drains is counted gap rows and
every key's latest value; an outbox that overflows while the bus is down is state `outbox` =
{"dropped": n} and the newest events once each, and one that overflows while attached reaches Central
as the same count without a reattach. Rollout skew: a projected key the display release's
table lacks is never sent and is logged, then written once a release lists it. A table change in one
epoch has Central put every key it owns again.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from integration.bus_servers import (
    STATE_TABLE,
    BusServer,
    FileLinkStore,
    Projection,
    central,
    desired_documents,
    hub_server,
    leaf_connections,
    line_slices,
    local,
    node_server,
    until,
)
from nats.js.errors import NotFoundError

from contracts.node_link import CENTRAL_WRITER, Pipe, account_id
from nodeapi.buffers import (
    KeyTable,
    Slice,
    desired_bucket,
    event_buffer,
    message_charge,
    state_bucket,
    table_of,
)
from nodeapi.documents import header_bytes
from nodeapi.envelope import event_headers, writer_of
from nodeapi.epoch import epoch_of
from nodeapi.hub import NodeLink
from nodeapi.node import OUTBOX_MESSAGES, NodeSession, Release

SERIAL = "serial-p"
MIB = 1024 * 1024
APPS = Release("1.0.0", "sha256:apps-release", {"apps": 1})
PLAYER = Release("1.0.0", "sha256:player-release", {"player": 1})
PLAYER_NEXT = Release("2.0.0", "sha256:player-next", {"player": 2})
PLAYER_STATE = "KV_state_player"
STATE_PREFIX = "$KV.state_player."
DISPLAY = Release("1.0.0", "sha256:display-release", {"display": 1})
DISPLAY_NEXT = Release("2.0.0", "sha256:display-next", {"display": 2})
DISPLAY_RECORDS = "RECORD_display"
DISPLAY_STATE = "KV_state_display"
DISPLAY_DESIRED = "KV_desired_display"


async def _linked(hub) -> None:
    async def check():
        return account_id(SERIAL) in leaf_connections(hub)
    await until(check, 15, "the Node's leaf links")


async def _held(jetstream, names) -> dict[str, str]:
    """name -> epoch once every named stream exists; {} until then."""
    held = {info.config.name: epoch_of(info) for info in await jetstream.streams_info()}
    return {name: held[name] for name in names} if set(names) <= held.keys() else {}


def _no_absent_stream(caplog) -> None:
    met = [record.getMessage() for record in caplog.records
           if record.name == "nodeapi.node" and "NoStreamResponse" in record.getMessage()]
    assert met == [], "a session met an absent stream"


def _born(store: FileLinkStore, epoch: str) -> dict | None:
    births = [json.loads(row["data"]) for row in store.records(PLAYER_STATE)
              if row["epoch"] == epoch and row["subject"] == STATE_PREFIX + "birth"]
    return births[-1] if births else None


def test_a_session_attaches_before_its_lines_declarer_and_writes_birth_in_every_epoch(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="nodeapi.node")
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    slices = line_slices()
    player_slice = slices["player"]
    records, state, desired = (config.name for config in player_slice.buffers)
    sessions: list[NodeSession] = []

    def session(component: str, slice_: Slice, release: Release) -> NodeSession:
        sessions.append(made := NodeSession(component, slice_, release, url=node.client_url))
        return made

    def emit(player: NodeSession, emitted: list[bytes], count: int) -> None:
        for _ in range(count):
            body = f"play-{len(emitted):03}".encode()
            player.events.emit("record.play", body, schema_major=1)
            emitted.append(body)

    hub.start()
    node.start()

    async def recorded(store: FileLinkStore, epochs: dict[str, str], emitted: list[bytes], digest: str) -> None:
        async def check():
            played = [row["data"] for row in store.records(records) if row["epoch"] == epochs[records]]
            birth = _born(store, epochs[state])
            return played == emitted and birth is not None and birth["slice_digest"] == digest
        await until(check, 20, f"the Player's birth and {len(emitted)} events recorded in its epoch")

    async def run():
        await _linked(hub)
        client = await central(hub, SERIAL)
        node_client = await local(node)
        jetstream = node_client.jetstream()
        store = FileLinkStore(tmp_path / "link.jsonl")
        # Central's show link runs throughout, from before any line exists: it finds each line it
        # did not list by the bus's stream names (erratum E-E3B-CC-1).
        stop = asyncio.Event()
        running = asyncio.create_task(NodeLink(client, Pipe.SHOW, store, Projection()).run(stop))
        try:
            # The Player starts first: nothing declares its line, so it waits, its events held.
            player = session("player", player_slice, PLAYER)
            emitted: list[bytes] = []
            player.start()
            emit(player, emitted, 10)
            await asyncio.sleep(3)
            assert not player.wait_attached(0)
            assert await _held(jetstream, (records, state, desired)) == {}

            # apps starts and applies the Player's line: the Player attaches in that epoch.
            apps = session("apps", slices["apps"], APPS)
            apps.apply_line(player_slice)
            apps.start()
            epochs = await until(lambda: _held(jetstream, (records, state, desired)), 15, "apps applies the Player line")
            assert await asyncio.to_thread(player.wait_attached, 15)
            await recorded(store, epochs, emitted, player_slice.digest)

            # The bus crashes and starts empty; the start order is reversed: apps re-attaches and
            # applies the held line again, then a Player starts. The link keeps running.
            player.stop()
            await node_client.close()
            node.crash()
            node.start()
            await _linked(hub)
            node_client = await local(node)
            jetstream = node_client.jetstream()

            async def reapplied():
                held = await _held(jetstream, (records, state, desired))
                return held if held and not set(held.values()) & set(epochs.values()) else {}
            epochs = await until(reapplied, 15, "apps re-applies the Player line in a new epoch")
            player = session("player", player_slice, PLAYER)
            player.start()
            emitted = []
            emit(player, emitted, 10)
            assert await asyncio.to_thread(player.wait_attached, 15)
            await recorded(store, epochs, emitted, player_slice.digest)
        finally:
            stop.set()
            await asyncio.wait_for(running, 15)
            for made in sessions:
                made.stop()
            await client.close()
            await node_client.close()

    try:
        asyncio.run(run())
    finally:
        for made in sessions:
            made.stop()
        node.stop()
        hub.stop()
    _no_absent_stream(caplog)


def test_apps_hot_swaps_the_player_line_through_the_union_of_its_held_slices(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="nodeapi.node")
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    old = Slice("player", (event_buffer("player", "record", 2 * MIB),
                           state_bucket("player", KeyTable({"track": 256, "legacy": 128}))))
    new = Slice("player", (event_buffer("player", "observation", 2 * MIB),
                           state_bucket("player", KeyTable({"track": 512, "volume": 64}))))
    # A release that needs a third stream of its own: with the old release held, four.
    fourth = Slice("player", (event_buffer("player", "observation", MIB),
                              state_bucket("player", KeyTable({"track": 512})),
                              desired_bucket("player", KeyTable({"show": 256}))))
    both = old.union(new)
    assert [config.name for config in both.buffers] == ["RECORD_player", PLAYER_STATE, "OBSERVATION_player"]
    assert table_of(both.buffers[1]).sizes == {"birth": 4096, "legacy": 128, "outbox": 4096, "track": 512,
                                                "volume": 64}
    sessions: list[NodeSession] = []
    hub.start()
    node.start()

    async def run():
        await _linked(hub)
        client = await central(hub, SERIAL)
        node_client = await local(node)
        jetstream = node_client.jetstream()
        store = FileLinkStore(tmp_path / "link.jsonl")
        try:
            apps = NodeSession("apps", line_slices()["apps"], APPS, url=node.client_url)
            sessions.append(apps)
            apps.start()
            assert await asyncio.to_thread(apps.wait_attached, 15)
            stop = asyncio.Event()   # before the line exists (erratum E-E3B-CC-1)
            running = asyncio.create_task(NodeLink(client, Pipe.SHOW, store, Projection()).run(stop))
            apps.apply_line(both)   # attached: applied now
            names = [config.name for config in both.buffers]
            epochs = await until(lambda: _held(jetstream, names), 15, "apps applies the union")

            # Both releases attach and play on the union.
            playing = NodeSession("player", old, PLAYER, url=node.client_url)
            starting = NodeSession("player", new, PLAYER_NEXT, url=node.client_url)
            sessions.extend((playing, starting))
            playing.state.put("legacy", b"old")
            playing.start()
            starting.start()
            assert await asyncio.to_thread(playing.wait_attached, 15)
            assert await asyncio.to_thread(starting.wait_attached, 15)
            for index in range(5):
                playing.events.emit("record.play", f"old-{index}".encode(), schema_major=1)
                starting.events.emit("observation.play", f"new-{index}".encode(), schema_major=2)

            async def drained():
                cursors = await store.cursors()
                ends = {name: (await jetstream.stream_info(name)).state.last_seq for name in names}
                return (len(store.records("RECORD_player")) == 5 and len(store.records("OBSERVATION_player")) == 5
                        and all(name in cursors and cursors[name].seq == end for name, end in ends.items())
                        and cursors)
            last = (await until(drained, 20, "Central drains both releases"))["RECORD_player"]

            # A next release whose union with the running one needs a fourth stream is refused before
            # anything is applied; the running slice stays.
            before = {info.config.name: (epoch_of(info), info.config) for info in await jetstream.streams_info()}
            with pytest.raises(ValueError, match="slice_past_its_line"):
                apps.apply_line(old.union(fourth))
            assert {info.config.name: (epoch_of(info), info.config)
                    for info in await jetstream.streams_info()} == before

            # The old release leaves; apps applies the new slice alone: the old-only stream is pruned
            # (Central records an unknown gap after the last event it read), the kept streams keep
            # their epochs, and the state table drops the old key.
            playing.stop()
            apps.apply_line(new)

            async def swapped():
                held = {info.config.name: info for info in await jetstream.streams_info()}
                return "RECORD_player" not in held and table_of(held[PLAYER_STATE].config) == table_of(
                    new.buffers[1]) and held
            held = await until(swapped, 15, "apps applies the new slice alone")
            assert {name: epoch_of(held[name]) for name in (PLAYER_STATE, "OBSERVATION_player")} == {
                name: epochs[name] for name in (PLAYER_STATE, "OBSERVATION_player")}

            async def pruned():
                return [row for row in store.rows("gap") if row["stream"] == "RECORD_player"]
            gaps = await until(pruned, 20, "Central records the pruned stream's gap")
            assert gaps == [{"kind": "gap", "stream": "RECORD_player", "epoch": last.epoch, "after": last.seq,
                             "count": None}]
            for index in range(5, 8):
                starting.events.emit("observation.play", f"new-{index}".encode(), schema_major=2)

            async def playing_on():
                return [row["data"] for row in store.records("OBSERVATION_player")] == [
                    f"new-{index}".encode() for index in range(8)]
            await until(playing_on, 15, "the new release keeps recording")
            # Both releases' births share one sticky key, so one may be superseded before Central reads
            # it: a counted gap, never an unknown one (§11 "state written faster").
            assert [row for row in store.rows("gap") if row["count"] is None] == gaps
            stop.set()
            await asyncio.wait_for(running, 15)
        finally:
            for made in sessions:
                made.stop()
            await client.close()
            await node_client.close()

    try:
        asyncio.run(run())
    finally:
        for made in sessions:
            made.stop()
        node.stop()
        hub.stop()
    _no_absent_stream(caplog)


class _Bed:
    """One hub and one Node with Central's fleet link and display sessions, for the loss, skew and
    table-change tests."""

    def __init__(self, tmp_path, node: BusServer, client, node_client) -> None:
        self.node, self.client, self.node_client = node, client, node_client
        self.store = FileLinkStore(tmp_path / "link.jsonl")
        self.projection = Projection()
        self.sessions: list[NodeSession] = []

    @property
    def jetstream(self):
        return self.node_client.jetstream()

    def session(self, slice_: Slice, release: Release = DISPLAY) -> NodeSession:
        made = NodeSession("display", slice_, release, url=self.node.client_url)
        self.sessions.append(made)
        made.start()
        return made

    def link(self) -> Callable[[], Awaitable[None]]:
        """Run Central's fleet link; the returned call stops it."""
        stop = asyncio.Event()
        running = asyncio.create_task(NodeLink(self.client, Pipe.FLEET, self.store, self.projection).run(stop))

        async def stopped() -> None:
            stop.set()
            await asyncio.wait_for(running, 15)
        return stopped

    async def drained(self, stream: str, what: str):
        """Central's cursor once it stands at the stream's last sequence."""
        async def check():
            try:
                end = (await self.jetstream.stream_info(stream)).state.last_seq
            except NotFoundError:   # not applied yet
                return None
            cursor = (await self.store.cursors()).get(stream)
            return cursor if cursor is not None and cursor.seq == end else None
        return await until(check, 20, what)

    async def documents(self, keys) -> dict:
        """key -> (value, writer, seq) of each key the Node's desired bucket holds."""
        held = {}
        for key in keys:
            with contextlib.suppress(NotFoundError):
                message = await self.jetstream.get_last_msg(DISPLAY_DESIRED, f"$KV.desired_display.{key}")
                held[key] = (message.data, writer_of(message.headers), message.seq)
        return held


@contextlib.asynccontextmanager
async def _bed(tmp_path) -> AsyncIterator[_Bed]:
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    hub.start()
    node.start()
    bed = None
    try:
        await _linked(hub)
        bed = _Bed(tmp_path, node, await central(hub, SERIAL), await local(node))
        yield bed
    finally:
        if bed is not None:
            for made in bed.sessions:
                made.stop()
            await bed.client.close()
            await bed.node_client.close()
        node.stop()
        hub.stop()


def _after(store: FileLinkStore, stream: str, cursor) -> list[dict]:
    """The stream's gap and record rows past `cursor` in its epoch, in commit order."""
    def past(row: dict) -> bool:
        if row["kind"] == "gap":
            return row["after"] >= cursor.seq
        return row["kind"] == "record" and row["seq"] > cursor.seq
    rows = [json.loads(line) for line in store.path.read_text().splitlines()]
    return [row for row in rows if past(row) and row["stream"] == stream and row["epoch"] == cursor.epoch]


def test_an_event_buffer_that_overflows_while_central_is_away_is_one_counted_gap(tmp_path):
    display = line_slices()["display"]
    records = display.buffers[0]

    async def run():
        async with _bed(tmp_path) as bed:
            stopped = bed.link()
            session = bed.session(display)
            for index in range(10):
                session.events.emit("record.shown", f"early-{index}".encode(), schema_major=1)
            cursor = await bed.drained(DISPLAY_RECORDS, "Central records the first events")
            await stopped()

            # Central is away: the session emits past the buffer's cap, every write accepted, the
            # oldest dropped by the server.
            count = records.max_bytes // 1024 + 200
            emitted = [f"late-{index:05}".encode().ljust(1024, b".") for index in range(count)]
            for body in emitted:
                session.events.emit("record.shown", body, schema_major=1)

            async def published():
                state = (await bed.jetstream.stream_info(DISPLAY_RECORDS)).state
                return state if state.last_seq == cursor.seq + count else None
            state = await until(published, 20, "the session publishes every event")
            dropped = state.first_seq - cursor.seq - 1
            assert dropped > 0 and state.messages == count - dropped

            bed.link()
            await bed.drained(DISPLAY_RECORDS, "Central drains the rest")
            rows = _after(bed.store, DISPLAY_RECORDS, cursor)
            assert rows[0] == {"kind": "gap", "stream": DISPLAY_RECORDS, "epoch": cursor.epoch, "after": cursor.seq,
                               "count": dropped}
            assert [row["kind"] for row in rows[1:]] == ["record"] * state.messages
            assert [row["seq"] for row in rows[1:]] == list(range(state.first_seq, state.last_seq + 1))
            assert [row["data"].encode("latin-1") for row in rows[1:]] == emitted[dropped:]
            assert bed.store.repeats == []

    asyncio.run(run())


def test_state_written_faster_than_central_drains_counts_the_unseen_revisions(tmp_path):
    display = line_slices()["display"]
    keys = [f"state{index}" for index in range(5)]
    prefix = "$KV.state_display."

    async def run():
        async with _bed(tmp_path) as bed:
            stopped = bed.link()
            session = bed.session(display)
            assert await asyncio.to_thread(session.wait_attached, 15)
            cursor = await bed.drained(DISPLAY_STATE, "Central records the session's birth")
            await stopped()

            latest: dict[str, bytes] = {}
            for index in range(500):
                key = keys[index % len(keys)]
                latest[key] = f"{key}-{index:03}".encode()
                session.state.put(key, latest[key])
                await asyncio.sleep(.002)

            async def published():
                for key, value in latest.items():
                    if (await bed.jetstream.get_last_msg(DISPLAY_STATE, prefix + key)).data != value:
                        return None
                return (await bed.jetstream.stream_info(DISPLAY_STATE)).state.last_seq
            end = await until(published, 20, "the session publishes every key's latest value")
            assert end - cursor.seq > STATE_TABLE.history * len(keys), "every revision is still held: no loss"

            bed.link()
            await bed.drained(DISPLAY_STATE, "Central drains the state")
            rows = _after(bed.store, DISPLAY_STATE, cursor)
            gaps = [row["count"] for row in rows if row["kind"] == "gap"]
            read = [row for row in rows if row["kind"] == "record"]
            assert gaps and None not in gaps
            assert sum(gaps) + len(read) == end - cursor.seq
            recorded = {row["subject"].removeprefix(prefix): row["data"].encode("latin-1") for row in read}
            assert {key: recorded.get(key) for key in keys} == latest

    asyncio.run(run())


def test_an_outbox_that_overflows_while_the_bus_is_down_is_counted_in_state(tmp_path):
    display = line_slices()["display"]
    records = display.buffers[0]
    emitted = [f"event-{index:05}".encode() for index in range(OUTBOX_MESSAGES + 100)]
    # The buffer holds every event the outbox keeps, so only the outbox can lose one.
    charge = message_charge("display.record.shown", len(emitted[0]), header_bytes(event_headers(1)))
    assert charge * OUTBOX_MESSAGES <= records.max_bytes

    async def run():
        async with _bed(tmp_path) as bed:
            bed.link()
            session = bed.session(display)
            assert await asyncio.to_thread(session.wait_attached, 15)
            before = {name: epoch_of(await bed.jetstream.stream_info(name)) for name in (DISPLAY_RECORDS, DISPLAY_STATE)}
            await bed.drained(DISPLAY_STATE, "Central records the session's birth")
            outboxes = [row for row in bed.store.records(DISPLAY_STATE) if row["subject"].endswith(".outbox")]
            assert [json.loads(row["data"]) for row in outboxes] == [{"dropped": 0}]

            # The bus stops (its memory store with it); the session emits while it is down.
            await bed.node_client.close()
            bed.node.stop()

            async def detached():
                return not session.wait_attached(0)
            await until(detached, 10, "the session sees the bus go")
            for body in emitted:
                session.events.emit("record.shown", body, schema_major=1)
            bed.node.start()
            bed.node_client = await local(bed.node)

            async def recorded():
                try:
                    epoch = epoch_of(await bed.jetstream.stream_info(DISPLAY_RECORDS))
                except NotFoundError:   # not applied yet
                    return None
                if epoch == before[DISPLAY_RECORDS]:
                    return None
                events = [row for row in bed.store.records(DISPLAY_RECORDS) if row["epoch"] == epoch]
                outbox = [json.loads(row["data"]) for row in bed.store.records(DISPLAY_STATE)
                          if row["subject"].endswith(".outbox") and row["epoch"] != before[DISPLAY_STATE]]
                return (events, outbox) if len(events) >= OUTBOX_MESSAGES and outbox else None
            events, outbox = await until(recorded, 30, "Central records the new epoch's events and outbox")
            assert outbox == [{"dropped": 100}]
            assert [row["data"] for row in events] == emitted[100:]
            assert len({row["message_id"] for row in events}) == OUTBOX_MESSAGES

    asyncio.run(run())


def test_an_outbox_that_overflows_while_attached_is_counted_in_state(tmp_path):
    display = line_slices()["display"]
    records = display.buffers[0]
    emitted = [f"event-{index:05}".encode() for index in range(OUTBOX_MESSAGES + 100)]
    # The buffer holds every event the outbox keeps, so only the outbox can lose one.
    charge = message_charge("display.record.shown", len(emitted[0]), header_bytes(event_headers(1)))
    assert charge * OUTBOX_MESSAGES <= records.max_bytes

    async def run():
        async with _bed(tmp_path) as bed:
            bed.link()
            session = bed.session(display)
            assert await asyncio.to_thread(session.wait_attached, 15)
            epoch = {name: epoch_of(await bed.jetstream.stream_info(name)) for name in (DISPLAY_RECORDS, DISPLAY_STATE)}
            await bed.drained(DISPLAY_STATE, "Central records the session's birth")
            outboxes = [row for row in bed.store.records(DISPLAY_STATE) if row["subject"].endswith(".outbox")]
            assert [json.loads(row["data"]) for row in outboxes] == [{"dropped": 0}]

            # The bus hangs with its connections kept (no reattach); the session overflows its outbox.
            bed.node.pause()
            try:
                for body in emitted:
                    session.events.emit("record.shown", body, schema_major=1)
            finally:
                bed.node.resume()

            async def counted():
                events = [row for row in bed.store.records(DISPLAY_RECORDS) if row["epoch"] == epoch[DISPLAY_RECORDS]]
                outbox = [json.loads(row["data"]) for row in bed.store.records(DISPLAY_STATE)
                          if row["subject"].endswith(".outbox")]
                return outbox[-1] if outbox[-1]["dropped"] + len(events) == len(emitted) else None
            outbox = await until(counted, 30, "Central's newest outbox count and records add up to every event")
            # One event may have been in flight when the outbox dropped it, then acknowledged (E-E3B-S6-2 a).
            assert outbox["dropped"] >= 99
            births = [row for row in bed.store.records(DISPLAY_STATE)
                      if row["subject"].endswith(".birth") and row["epoch"] == epoch[DISPLAY_STATE]]
            assert len(births) == 1, "the session never reattached"

    asyncio.run(run())


def _display(documents: KeyTable) -> Slice:
    """The display line's harness slice with another desired-bucket table."""
    records, state, _ = line_slices()["display"].buffers
    return Slice("display", (records, state, desired_bucket("display", documents)))


def test_a_key_the_nodes_table_lacks_is_never_sent_and_written_once_a_release_lists_it(tmp_path):
    shown = _display(KeyTable({"show": 4096}, history=2))
    listing = line_slices()["display"]   # its table lists `layout`
    assert "layout" in table_of(listing.buffers[2]).sizes

    async def run():
        async with _bed(tmp_path) as bed:
            bed.projection.streams[DISPLAY_DESIRED] = {"show": b"show-1", "layout": b"layout-1"}
            bed.link()
            session = bed.session(shown)

            async def refused():
                return any(body == {"stream": DISPLAY_DESIRED, "key": "layout", "reason": "document_unlisted"}
                           for body in bed.store.actions("document_refused"))
            await until(refused, 20, "Central refuses the unlisted key and logs it")
            assert (await bed.documents(("show", "layout"))).keys() == {"show"}

            # A release that lists `layout` re-attaches (a new birth); Central's next reconcile writes it.
            session.stop()
            session = bed.session(listing, DISPLAY_NEXT)

            async def written():
                held = await bed.documents(("show", "layout"))
                return held if held.keys() == {"show", "layout"} else None
            held = await until(written, 20, "Central writes the newly listed key")
            assert {key: value[:2] for key, value in held.items()} == {
                "show": (b"show-1", CENTRAL_WRITER), "layout": (b"layout-1", CENTRAL_WRITER)}
            births = [json.loads(row["data"]) for row in bed.store.records(DISPLAY_STATE)
                      if row["subject"].endswith(".birth")]
            assert births[-1]["version"] == DISPLAY_NEXT.version

    asyncio.run(run())


def test_a_changed_key_table_has_central_put_every_key_it_owns_again(tmp_path):
    documents = desired_documents("display")
    first = _display(documents)
    second = _display(KeyTable({**documents.sizes, "show": 1024, "extra": 512}, documents.history))
    projected = {"show": b"show-1", "layout": b"layout-1", "frame00": b"frame-1"}

    async def run():
        async with _bed(tmp_path) as bed:
            bed.projection.streams[DISPLAY_DESIRED] = dict(projected)
            bed.link()
            session = bed.session(first)

            async def written():
                held = await bed.documents(projected)
                return held if held.keys() == projected.keys() else None
            before = await until(written, 20, "Central writes its documents")
            epoch = epoch_of(await bed.jetstream.stream_info(DISPLAY_DESIRED))

            # A release shrinks `show` and adds `extra`, in the same epoch: Central puts every key
            # it owns again, and the new one.
            bed.projection.streams[DISPLAY_DESIRED]["extra"] = b"extra-1"
            session.stop()
            bed.session(second, DISPLAY_NEXT)

            async def rewritten():
                held = await bed.documents((*projected, "extra"))
                return held if held.keys() == {*projected, "extra"} and all(
                    held[key][2] > before[key][2] for key in projected) else None
            held = await until(rewritten, 20, "Central puts every owned key again")
            info = await bed.jetstream.stream_info(DISPLAY_DESIRED)
            assert epoch_of(info) == epoch and table_of(info.config) == table_of(second.buffers[2])
            assert {key: value[:2] for key, value in held.items()} == {
                key: (value, CENTRAL_WRITER) for key, value in {**projected, "extra": b"extra-1"}.items()}

    asyncio.run(run())
