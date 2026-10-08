"""A Node bus started with the environment its handoff stage writes links its leaf to the hub
through the boot origin's path prefix (E3c S1; erratum E-E3C-CUT-5).

The hub runs Fleet's generated configuration; a path-prefix proxy at `LEAF_PATH` stands in for the
origin's ingress route; the Node runs the shipped `node-bus.conf` with
`bus_environment(<origin>, SERIAL)`, only its client port replaced by a free one (on a Node it is
NODE_BUS_PORT, which tests running side by side cannot share).
"""
from __future__ import annotations

import asyncio
import shutil

from integration.bus_servers import (
    NODE_BUS_CONF,
    BusServer,
    PrefixProxy,
    _free_port,
    central,
    hub_server,
    leaf_connections,
    until,
)
from node_pid1_bus_probe import server_info

from appliance.boot.bus_environment import bus_environment
from contracts.node_link import LEAF_PATH, NODE_DOMAIN, account_id, node_user

SERIAL = "serial-origin"


def test_a_node_bus_links_from_its_boot_origin_and_serial(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    proxy = PrefixProxy(hub.websocket_port, LEAF_PATH)
    directory = tmp_path / "node"
    directory.mkdir()
    config = directory / "node-bus.conf"
    shutil.copyfile(NODE_BUS_CONF, config)
    port = _free_port()
    environment = {**bus_environment(f"http://127.0.0.1:{proxy.port}", SERIAL),
                   "PHOTO_WALL_BUS_PORT": str(port)}
    node = BusServer(name="node", config=config, client_url=f"nats://127.0.0.1:{port}",
                     environment=environment)
    hub.start()

    async def run():
        await proxy.start()
        node.start()
        try:
            async def linked():
                return account_id(SERIAL) in leaf_connections(hub)
            await until(linked, 10, "the Node's leaf at the hub")
            assert proxy.paths == [f"/{LEAF_PATH}/leafnode"]
            assert server_info(port)["server_name"] == node_user(SERIAL)
            central_client = await central(hub, SERIAL)
            try:
                info = await central_client.jetstream(domain=NODE_DOMAIN).account_info()
                assert info.domain == NODE_DOMAIN
            finally:
                await central_client.close()
        finally:
            node.stop()
            await proxy.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()
