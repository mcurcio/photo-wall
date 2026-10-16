"""HostCore on the Node's bus (E3c S4; E3b design §7.3, §9.5; errata E-E3C-CUT-2, -3), and the Node's
read-only door on the home network (0017 C3, the Home Assistant system design).

The host component's session declares the host line and writes `birth` and `base` at every attach, so
a bus killed with SIGKILL (it restarts empty: the memory store) holds both again in a new epoch with
no help from Central. That the installed HostCore launcher imports its session from the package
directories its PATH names, with python3-nats, is tests/debs/test_node_package.py's (decision 0019).

The door: a client on the shipped configuration's WebSocket listener with no credentials reads
HostCore's `base` by one stored-message get and sees the bus's server name; the server refuses its
writes (a key put, a stream delete or purge) and the stored value stands; the local user is refused
there.
"""
from __future__ import annotations

import asyncio

import nats.errors
import pytest
from integration.bus_servers import door, door_get, hub_server, local, node_server, until
from nats.js.errors import NotFoundError
from node_pid1_bus_probe import host_state

from appliance.host.bus import HOST_SLICE, host_session
from contracts.node_link import bus_server_name
from contracts.node_state import HostBase, decode_host_base

SERIAL = "serial-h"
RECORDS = "RECORD_host"
STATE = "KV_state_host"
BASE_SUBJECT = "$KV.state_host.base"
BASE_TAG = "base-test"


async def _held(node) -> dict | None:
    """The probe's {"birth", "base", "epoch"} (the node-pid1 leg reads the same) once both keys are
    there, else None."""
    try:
        client = await local(node)
    except (OSError, nats.errors.Error, asyncio.TimeoutError):   # the bus is still coming back
        return None
    try:
        return await host_state(client)
    except NotFoundError:
        return None
    finally:
        await client.close()


def test_the_host_component_writes_birth_and_refills_it_after_a_bus_crash(tmp_path):
    node = node_server(tmp_path, SERIAL, hub_server(tmp_path, [SERIAL]))   # its hub is never started
    node.start()
    session = host_session(BASE_TAG, url=node.client_url)
    session.start()
    try:
        async def first():
            return await until(lambda: _held(node), 10, "the host's birth and base")

        before = asyncio.run(first())
        assert before["birth"]["component"] == "host" and before["birth"]["pipe"] == "fleet"
        assert before["birth"]["release_digest"] == BASE_TAG
        assert before["birth"]["slice_digest"] == HOST_SLICE.digest
        assert before["base"] == {"base_tag": BASE_TAG}

        node.crash()
        node.start()

        async def refilled():
            async def check():
                held = await _held(node)
                return held if held is not None and held["epoch"] != before["epoch"] else None
            held = await until(check, 10, "the host's birth and base in a new epoch")
            client = await local(node)
            try:
                await client.jetstream().stream_info(RECORDS)
            finally:
                await client.close()
            return held

        after = asyncio.run(refilled())
        assert {key: after[key] for key in ("birth", "base")} == {key: before[key] for key in ("birth", "base")}
    finally:
        session.stop()
        node.stop()


def test_the_door_reads_the_pis_values_and_refuses_every_write(tmp_path):
    """Mutation probes: widen reader's publish allow to ">" -> the writes are not refused; drop
    `allowed_connection_types` from `local` -> local over the door is admitted."""
    node = node_server(tmp_path, SERIAL, hub_server(tmp_path, [SERIAL]))   # its hub is never started
    node.start()
    session = host_session(BASE_TAG, url=node.client_url)
    session.start()

    async def run():
        await until(lambda: _held(node), 10, "the host's birth and base")
        refused: list[Exception] = []

        async def on_error(error: Exception) -> None:
            refused.append(error)

        reader = await door(node, error_cb=on_error)
        try:
            # It reached the Pi it meant: the door's INFO names the bus.
            assert reader._server_info["server_name"] == bus_server_name(SERIAL)
            base = await door_get(reader, STATE, BASE_SUBJECT)
            assert base is not None and decode_host_base(base) == HostBase(base_tag=BASE_TAG)

            writes = ((BASE_SUBJECT, b'{"base_tag":"forged"}'), (f"$JS.API.STREAM.DELETE.{STATE}", b""),
                      (f"$JS.API.STREAM.PURGE.{STATE}", b""))
            for subject, payload in writes:
                await reader.publish(subject, payload)
            await reader.flush()

            async def all_refused():
                return len(refused) >= len(writes)
            await until(all_refused, 5, "the server refusing every write through the door")
            assert all("permissions violation" in str(error).lower() for error in refused), refused
            assert sorted(next(subject for subject, _ in writes if subject.lower() in str(error)) for error in refused) == \
                sorted(subject for subject, _ in writes)
            assert decode_host_base(await door_get(reader, STATE, BASE_SUBJECT)) == HostBase(base_tag=BASE_TAG)
        finally:
            await reader.close()
        held = await _held(node)
        assert held is not None and held["base"] == {"base_tag": BASE_TAG}

        # The local user is admitted only on the loopback client port.
        with pytest.raises((nats.errors.Error, OSError)):
            await door(node, user="local", password="local")

    try:
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()
