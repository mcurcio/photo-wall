"""The bus harness itself: a server counts as started only on its own word, and no port is handed out
twice. A port another process holds is fatal to nats-server, so before `BusServer.start` checked the
server's ports file a Node that lost its port exited while `start()` returned, and the test timed out
waiting for a leaf link that could never come (CI job 112634214274). The port tests need no server.
"""
from __future__ import annotations

import asyncio
import dataclasses
import socket

import integration.bus_servers as bus_servers
import pytest
from integration.bus_servers import PORT_CEILING, PORT_FLOOR, hub_server, node_server, until


def test_node_bus_a_node_whose_port_another_server_holds_fails_to_start(tmp_path):
    hub = hub_server(tmp_path, ["serial-a", "serial-b"])
    node_a = node_server(tmp_path, "serial-a", hub)
    node_b = node_server(tmp_path, "serial-b", hub)
    taken = str(bus_servers._port_of(node_a.client_url))
    node_b = dataclasses.replace(node_b, client_url=f"nats://127.0.0.1:{taken}",
                                 environment={**node_b.environment, "PHOTO_WALL_BUS_PORT": taken})
    try:
        hub.start()
        node_a.start()
        with pytest.raises(RuntimeError, match=r"(?s)node serial-b exited .*FTL.*address already in use"):
            node_b.start()
        assert node_b.state() == "stopped"
    finally:
        node_b.stop()
        node_a.stop()
        hub.stop()


def test_node_bus_a_hub_whose_leaf_port_is_held_fails_to_start(tmp_path):
    """The ports file lists no leafnodes port; the leaf listener binds before the file is written and
    failing to bind it is fatal, so a held leaf port still fails `start()`."""
    hub = hub_server(tmp_path, ["serial-a"])
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", hub.listeners.leaf_port))
        holder.listen()
        try:
            with pytest.raises(RuntimeError, match=r"(?s)hub exited .*FTL"):
                hub.start()
        finally:
            hub.stop()


def test_node_bus_until_times_out_with_every_started_servers_log(tmp_path):
    hub = hub_server(tmp_path, ["serial-a"])

    async def never():
        return False
    try:
        hub.start()
        with pytest.raises(AssertionError, match=r"(?s)not within 0.1s: nothing.*--- hub \(running\):.*ready"):
            asyncio.run(until(never, .1, "nothing"))
    finally:
        hub.stop()


def test_node_bus_free_port_redraws_a_port_it_already_issued(monkeypatch):
    first, second = 21000, 21001
    draws = iter([first, first, second])
    monkeypatch.setattr(bus_servers, "_ISSUED", set())
    monkeypatch.setattr(bus_servers, "_draw", lambda: next(draws))
    assert [bus_servers._free_port(), bus_servers._free_port()] == [first, second]


def test_node_bus_each_xdist_worker_draws_from_its_own_slice_below_the_ephemeral_floors(monkeypatch):
    slices = []
    for index in range(4):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", f"gw{index}")
        monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "4")
        slices.append(set(bus_servers._port_slice()))
    assert all(slices) and sum(map(len, slices)) == len(set().union(*slices))
    assert min(map(min, slices)) >= PORT_FLOOR and max(map(max, slices)) < PORT_CEILING <= 32768
