"""The install tracer (the Home Assistant system design, draft 4, Q2 A: "each node can use its own
version"): Install on one Pi, called as Home Assistant calls it at Central's door, changes only that
Pi's next boot offer, and its installed version follows the base that Pi boots.

Real composition: Central's app under uvicorn (its door relayed to the hub), the worker as
`media.worker` runs it with its Node bus from `build_node_bus` (Central's hub session inside, serving
`versions` and `installPlayer`), two stable releases ingested by the release sync from a mock GitHub
upstream with their files in Central's cache, two Pis enrolled by their boot offers, and Pi A's Node
on the shipped node-bus.conf with HostCore's bus (appliance/host/bus.py `host_session`) on the base
its offer named.

1. Update the wall (PUT /v1/operator/releases/wall) puts both Pis on the older release.
2. As Home Assistant: installPlayer(A, newer) answers InstallStarted.
3. A's next boot offer is the newer release's base, B's still the older one's; Central's `versions`
   shows A chosen at the newer release and B unchanged.
4. HostCore on A's Node, on the base its offer named: its `base`, read through A's read-only door,
   is the newer release (the Player device's installed version follows its Pi).
5. installPlayer for a serial Central never enrolled answers InstallRefused(unknown_player).
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

from integration.bus_servers import hub_server, node_server, until
from integration.home_assistant_rig import CentralOrigin, PiReader, central_state, worker
from integration.room_light_rig import HomeAssistant
from test_node_release_catalog import store_files
from test_node_release_ingest import Upstream, make_sync_world, node_upload, sync

from appliance.host.bus import host_session
from contracts.central_api import (
    VERSIONS_KEY,
    InstallRefusal,
    InstallRefused,
    InstallStarted,
    Versions,
)
from contracts.node_boot import (
    NodeBootOfferV2,
    NodeBootRequestV2,
    encode_node_boot_request,
    parse_node_boot_offer,
)
from contracts.node_state import HOST_BASE_KEY, HOST_LINE, decode_host_base
from nodeapi.buffers import state_prefix, state_stream

A, B = "serial-install-a", "serial-install-b"
OLDER, NEWER = "v2.0.0", "v2.1.0"
FIRST_SECONDS = 30   # the worker reaches the hub and its hub session puts Central's state
SECONDS = 10


def _boot(central: CentralOrigin, serial: str) -> NodeBootOfferV2:
    """A fresh boot of `serial` (a new kernel boot id and nonce) asks Central for its offer."""
    request = NodeBootRequestV2(serial, uuid4(), uuid4().hex + uuid4().hex)
    answer = central.call("POST", "/v2/node/boot-offers", content=encode_node_boot_request(request), headers={})
    return parse_node_boot_offer(answer.content)


def test_install_on_one_pi_changes_only_that_pis_boot_and_installed_version(registry, tmp_path):
    database = registry.db
    upstream, older, newer = Upstream(), node_upload(OLDER), node_upload(NEWER)
    for upload in (older, newer):
        upstream.put(upload)
    world = make_sync_world(registry, tmp_path)(upstream)
    sync(world)
    for upload in (older, newer):
        store_files(world, upload)
    sync(world)

    hub = hub_server(tmp_path, [A, B])
    node = node_server(tmp_path, A, hub)
    central = CentralOrigin(database, registry.clock, hub, cache_root=tmp_path)
    stoppers: list = []

    async def run() -> None:
        # 1. Two Pis enrolled by their boot offers, then both put on the older release.
        for serial in (A, B):
            await asyncio.to_thread(_boot, central, serial)
        await asyncio.to_thread(central.call, "PUT", "/v1/operator/releases/wall", json={"version": OLDER})
        assert (await asyncio.to_thread(_boot, central, A)).base.tag == OLDER

        running = worker(database.dsn, hub, tmp_path)
        running.start()
        stoppers.append(running.stop)
        async with HomeAssistant(central.door_url) as house:
            def versions(check):
                async def read() -> Versions | None:
                    value = await central_state(house, VERSIONS_KEY)
                    return value if value is not None and {A, B} <= value.pis.keys() and check(value) else None
                return read
            await until(versions(lambda v: (v.pis[A].version, v.pis[B].version) == (OLDER, OLDER)), FIRST_SECONDS,
                        "Central's versions show both Pis on the older release")

            # 2. installPlayer, as Home Assistant calls it.
            started = await house.install(A, NEWER)
            assert isinstance(started, InstallStarted), started
            assert (started.serial, started.version) == (A, NEWER)

            # 3. Only A's next boot moves; `versions` says the same.
            offer_a = await asyncio.to_thread(_boot, central, A)
            offer_b = await asyncio.to_thread(_boot, central, B)
            assert (offer_a.base.tag, offer_b.base.tag) == (NEWER, OLDER)
            shown = await until(versions(lambda v: v.pis[A].version == NEWER), SECONDS,
                                "Central's versions show A at the newer release")
            assert shown.pis[A].chosen
            assert (shown.pis[B].version, shown.pis[B].chosen, shown.pis[B].install) == (OLDER, True, None)

            # 4. HostCore on A's Node, on the base A's offer named: its installed version, through A's door.
            host = host_session(offer_a.base.tag, url=node.client_url)
            host.start()
            stoppers.append(lambda: asyncio.to_thread(host.stop))
            reader = await PiReader.at(node, A, SECONDS)
            stoppers.append(reader.close)

            async def installed():
                raw = await reader.get(state_stream(HOST_LINE), state_prefix(HOST_LINE) + HOST_BASE_KEY)
                return None if raw is None else decode_host_base(raw)
            assert (await until(installed, SECONDS, "A's host line holds its base")).base_tag == NEWER

            # 5. A serial Central never enrolled.
            assert await house.install("serial-never-enrolled", NEWER) == InstallRefused(
                refusal=InstallRefusal.UNKNOWN_PLAYER)
        assert running.running()

    async def main() -> None:
        try:
            await run()
        finally:
            for stop in reversed(stoppers):
                await stop()

    try:
        hub.start()
        node.start()
        central.start()
        asyncio.run(main())
    finally:
        central.stop()
        node.stop()
        hub.stop()
