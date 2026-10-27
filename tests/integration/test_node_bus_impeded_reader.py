"""Display Host reads the Player's `impeded` report on the Node's bus (the Frame shows Central's
error when it blacks the Frame; DESIGN-r8 §5): `BusImpededReports` over its own `StateReader`, on
the shipped Node configuration, with a Player-line session as the writer. The report reaches the
controller and every change after it, and the Player line's other keys never call back; after the
Node's bus restarts, the last report stays until the Player restates (its grant names the run, so
an ended run's report counts for nothing: tests/node/display/test_display_central_card.py).
"""
from __future__ import annotations

import asyncio

from integration.bus_servers import hub_server, node_server, until

from appliance.display_host.bus import BusImpededReports
from contracts.node_impeded import (
    IMPEDED_BYTES,
    IMPEDED_KEY,
    Impeded,
    ImpededFrames,
    encode_impeded,
)
from contracts.node_link import STORE_LINES
from contracts.node_state import PLAYER_COMPONENT, PLAYER_LINE
from contracts.room_light import CentralWords
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.node import NodeSession, Release

SERIAL = "serial-i"
GRANT = "6f1c2a4e-1b2d-4c3e-8f40-5a6b7c8d9e0f"
WORDS = CentralWords(line1="Photo Wall can't read Family Room · since 26 Oct 15:35",
                     line2="Drawn with its last word: off · see Needs attention")
IMPEDED = ImpededFrames(frames=(Impeded(frame_id="frame-1", output_id="HDMI-A-1", grant_id=GRANT,
                                        lights=("family-room",), words=WORDS),))
CLEAR = ImpededFrames(frames=())


def player_session(url: str) -> NodeSession:
    """A Player-line writer of `impeded` (and one other key) on the Player's own store line."""
    state = state_bucket(PLAYER_LINE, KeyTable({IMPEDED_KEY: IMPEDED_BYTES, "other": 64}))
    records = event_buffer(PLAYER_LINE, "record", STORE_LINES[PLAYER_LINE].max_bytes - state.max_bytes)
    return NodeSession(PLAYER_COMPONENT, Slice(PLAYER_LINE, (records, state)),
                       Release("1.0.0", "d" * 64, {PLAYER_LINE: 1}), url=url)


def test_the_controller_reads_the_players_report_and_each_change(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    node.start()
    reports = BusImpededReports(url=node.client_url)
    told: list[ImpededFrames | None] = []
    reports.on_change(lambda: told.append(reports.latest()))
    player = player_session(node.client_url)

    async def run():
        reports.start()                         # before the Player's line exists: it waits
        player.start()
        player.state.put(IMPEDED_KEY, encode_impeded(IMPEDED))

        async def holds(wanted):
            return reports.latest() == wanted
        await until(lambda: holds(IMPEDED), 20, "the controller reads the Player's report")
        player.state.put("other", b"1")
        player.state.put(IMPEDED_KEY, encode_impeded(CLEAR))
        await until(lambda: holds(CLEAR), 20, "the controller reads the light back on")
        assert told == [IMPEDED, CLEAR]         # the other key never called back

        player.stop()
        node.stop()                             # the Node's bus restarts empty
        node.start()
        await asyncio.sleep(1)
        assert reports.latest() == CLEAR        # last read stays until the Player restates
        restarted = player_session(node.client_url)
        restarted.start()
        restarted.state.put(IMPEDED_KEY, encode_impeded(IMPEDED))
        try:
            await until(lambda: holds(IMPEDED), 20, "the restated report after a bus restart")
        finally:
            restarted.stop()

    try:
        asyncio.run(run())
    finally:
        reports.stop()
        player.stop()
        node.stop()
