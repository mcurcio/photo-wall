"""HostCore on the Node's bus (E3c S4; E3b design §7.3, §9.5; errata E-E3C-CUT-2, -3).

The host component's session declares the host line and writes `birth` and `base` at every attach, so
a bus killed with SIGKILL (it restarts empty: the memory store) holds both again in a new epoch with
no help from Central. That the installed HostCore launcher imports its session from the package
directories its PATH names, with python3-nats, is tests/debs/test_node_package.py's (decision 0019).
"""
from __future__ import annotations

import asyncio

import nats.errors
from integration.bus_servers import hub_server, local, node_server, until
from nats.js.errors import NotFoundError
from node_pid1_bus_probe import host_state

from appliance.host.bus import HOST_SLICE, host_session

SERIAL = "serial-h"
RECORDS = "RECORD_host"
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

