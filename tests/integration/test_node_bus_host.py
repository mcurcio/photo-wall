"""HostCore on the Node's bus (E3c S4; E3b design §7.3, §9.5; errata E-E3C-CUT-2, -3).

The host component's session declares the host line and writes `birth` and `base` at every attach, so
a bus killed with SIGKILL (it restarts empty: the memory store) holds both again in a new epoch with
no help from Central. The base package ships what that session needs inside HostCore's own launcher
directory: `nodeapi`, `contracts` and the nats-py wheel `uv.lock` pins, imported from there alone.
"""
from __future__ import annotations

import asyncio

import nats.errors
from integration.bus_servers import hub_server, local, nats_server_binary, node_server, until
from nats.js.errors import NotFoundError
from node_pid1_bus_probe import host_state
from support.repo import REPO

from appliance.host.bus import HOST_SLICE, host_session
from scripts.build_node_base_deb import BUS_DIRECTORY, POLICIES, stage_package
from scripts.module_closure import isolated_import

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


def test_the_staged_host_core_imports_nats_and_nodeapi_from_its_own_directory(tmp_path, tmp_path_factory):
    nats_server_binary()   # the node-bus tier owns this test: it fetches the pinned downloads
    downloads = tmp_path_factory.getbasetemp().parent / "downloads"   # shared by xdist workers and runs
    stage_package(REPO, tmp_path / "package", downloads)
    report = isolated_import(tmp_path / "package/usr/lib/photo-wall-host-core",
                             ["nats", "nodeapi.node", "appliance.host.bus"], policy=POLICIES["host-core"])
    assert report.imported == ("nats", "nodeapi.node", "appliance.host.bus") and not report.unavailable


def test_the_staged_base_ships_the_bus_servers_licence_beside_it(tmp_path, tmp_path_factory):
    # nats-server is third-party Apache-2.0 code: the base carries its licence wherever it carries the
    # binary, as the vendored wheel's dist-info carries nats-py's (erratum E-E3C-S1-4).
    nats_server_binary()
    downloads = tmp_path_factory.getbasetemp().parent / "downloads"
    stage_package(REPO, tmp_path / "package", downloads)
    bus = tmp_path / "package" / BUS_DIRECTORY
    assert (bus / "nats-server").is_file()
    licence = (bus / "LICENSE").read_text()
    assert licence.split()[:4] == ["Apache", "License", "Version", "2.0,"], licence[:200]
