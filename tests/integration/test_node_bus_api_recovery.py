"""A Node session whose line another component declares, on real servers (E3b design §7.2 node, §7.3,
§9.5, §9.7, §11 attach row, §16.1 "also in E3b"; erratum E-E3B-R2-4).

The Player's store line is declared by apps, from the held Player releases' slices; the Player
session never applies it. Attach before declare: the Player starts first and its events wait in the
outbox; once apps applies the line, the Player attaches and Central's show NodeLink records its
`birth` and every event in that epoch, and again in the next epoch after a bus crash with the start
order reversed. Hot-swap: apps applies the union of the old and new releases' slices, both Player
sessions attach, then apps applies the new slice alone; the old-only stream is pruned and Central
records an unknown gap for it. A release whose union with the running one does not fit the line is
refused before anything is applied. No session meets an absent stream on the way.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from integration.bus_servers import (
    FileLinkStore,
    Projection,
    central,
    hub_server,
    leaf_connections,
    line_slices,
    local,
    node_server,
    until,
)

from contracts.node_link import Pipe, account_id
from nodeapi.buffers import KeyTable, Slice, desired_bucket, event_buffer, state_bucket, table_of
from nodeapi.epoch import epoch_of
from nodeapi.hub import NodeLink
from nodeapi.node import NodeSession, Release

SERIAL = "serial-p"
MIB = 1024 * 1024
APPS = Release("1.0.0", "sha256:apps-release", {"apps": 1})
PLAYER = Release("1.0.0", "sha256:player-release", {"player": 1})
PLAYER_NEXT = Release("2.0.0", "sha256:player-next", {"player": 2})
PLAYER_STATE = "KV_state_player"
STATE_PREFIX = "$KV.state_player."


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
            # The show link starts once the line exists: a line created after a link's reconcile is
            # found only at its next one (erratum E-E3B-S4-1, not this slice's).
            stop = asyncio.Event()
            running = asyncio.create_task(NodeLink(client, Pipe.SHOW, store, Projection()).run(stop))
            await recorded(store, epochs, emitted, player_slice.digest)

            # The bus crashes and starts empty; the start order is reversed: apps re-attaches and
            # applies the held line again, then a Player starts.
            stop.set()
            await asyncio.wait_for(running, 15)
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
            stop = asyncio.Event()
            running = asyncio.create_task(NodeLink(client, Pipe.SHOW, store, Projection()).run(stop))
            player = session("player", player_slice, PLAYER)
            player.start()
            emitted = []
            emit(player, emitted, 10)
            assert await asyncio.to_thread(player.wait_attached, 15)
            await recorded(store, epochs, emitted, player_slice.digest)
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
            apps.apply_line(both)   # attached: applied now
            names = [config.name for config in both.buffers]
            epochs = await until(lambda: _held(jetstream, names), 15, "apps applies the union")
            stop = asyncio.Event()   # after the line exists (erratum E-E3B-S4-1)
            running = asyncio.create_task(NodeLink(client, Pipe.SHOW, store, Projection()).run(stop))

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
