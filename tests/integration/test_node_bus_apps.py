"""The app broker on the Node's bus (decision 0036, "Restart app"; contracts/node_apps.py), over a
real nats-server pair: Central's fleet-pipe NodeLink calls the broker's `restart` across the leaf,
the method answers from the slot alone (one restart for two requests, no request served twice), and
the steps the broker states are records on its line and the line's newest state.

The broker's loop (one stop, one start, never repeated) is tests/node/apps/test_app_restart.py's;
Central's job and its progress read are tests/test_app_restart.py's.
"""
from __future__ import annotations

import asyncio
import contextlib

import nats.errors
from integration.bus_servers import (
    FileLinkStore,
    Projection,
    central,
    hub_server,
    local,
    node_server,
    until,
)
from nats.js.errors import NotFoundError

from appliance.apps.bus import BusRestartLine, apps_session
from appliance.apps.restart import RestartAsks
from contracts.node_apps import (
    APP_RESTART_KEY,
    APP_RESTART_METHOD,
    APP_RESTART_RECORD,
    APPS_LINE,
    AppRestart,
    AppRestartOutcome,
    AppRestartState,
    AppRestartStep,
    decode_app_restart_answer,
    decode_app_restart_state,
    encode_app_restart,
)
from contracts.node_link import Pipe
from nodeapi.hub import HubUnavailable, NodeLink

SERIAL = "serial-a"
FIRST, SECOND = "0b0f6d6e-2f0f-4d53-9d0b-1c7f8a5e0001", "0b0f6d6e-2f0f-4d53-9d0b-1c7f8a5e0002"


def test_central_asks_restart_across_the_leaf_and_reads_the_brokers_steps(tmp_path):
    """Mutation probe: hold every accepted request without the in-flight answer (`RestartAsks.ask`
    always "accepted") -> the second request is accepted too, fails."""
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub)
    asks = RestartAsks()
    session = apps_session("digest", asks, url=node.client_url)
    line = BusRestartLine(session)

    async def run():
        client = await central(hub, SERIAL)
        try:
            store = FileLinkStore(tmp_path / "link.jsonl")
            link = NodeLink(client, Pipe.FLEET, store, Projection())

            async def ask(request_id: str) -> AppRestartOutcome:
                reply = await link.call(APPS_LINE, APP_RESTART_METHOD,
                                        encode_app_restart(AppRestart(request_id=request_id)), timeout=1)
                return decode_app_restart_answer(reply).outcome

            async def answered():
                with contextlib.suppress(HubUnavailable):
                    return await ask(FIRST)
            assert await until(answered, 20, "the broker answers restart across the leaf") is AppRestartOutcome.ACCEPTED
            assert await ask(SECOND) is AppRestartOutcome.IN_FLIGHT
            assert await ask(FIRST) is AppRestartOutcome.IN_FLIGHT
            assert asks.take() == FIRST and asks.take() is None        # one request for the loop
            assert {"component": "apps", "method": "restart", "outcome": "ok"} in store.actions("call")

            await link.call(APPS_LINE, APP_RESTART_METHOD, b'{"why":"no request"}', timeout=1)
            assert {"component": "apps", "method": "restart", "outcome": "error 500"} in store.actions("call")
            assert asks.take() is None                                 # it restarts nothing

            line.state(FIRST, "app_stopped")                           # as the loop states them
            line.state(FIRST, "app_started")
            asks.settle(FIRST)
            assert await ask(FIRST) is AppRestartOutcome.DUPLICATE

            program = await local(node)
            try:
                async def stated():
                    try:
                        bucket = await program.jetstream().key_value("state_apps")
                        return decode_app_restart_state((await bucket.get(APP_RESTART_KEY)).value)
                    except (NotFoundError, nats.errors.Error):
                        return None
                started = AppRestartState(request_id=FIRST, step=AppRestartStep.APP_STARTED)

                async def newest():
                    return await stated() == started
                await until(newest, 10, "the line's newest restart step")
                records = await program.jetstream().subscribe(f"{APPS_LINE}.{APP_RESTART_RECORD}",
                                                              ordered_consumer=True)
                steps = [decode_app_restart_state((await records.next_msg(timeout=5)).data) for _ in range(2)]
                assert steps == [AppRestartState(request_id=FIRST, step=AppRestartStep.APP_STOPPED), started]
            finally:
                await program.close()
        finally:
            await client.close()

    hub.start()
    node.start()
    session.start()
    try:
        asyncio.run(run())
    finally:
        session.stop()
        node.stop()
        hub.stop()
