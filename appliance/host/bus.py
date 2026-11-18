"""The host component on its Node's bus.

HostCore declares the host store line: a circular record buffer and its state bucket, which holds
`birth`, `outbox` and two keys of its own: `base` (contracts/node_state.py `HostBase`, the base this
boot runs) and, on a Pi with a wired light meter, LIGHT_SENSOR_KEY (contracts/room_light.py
`LightSensorReading`; put by appliance/host/light_sensor.py `start_light_meter`). Anyone reads them:
Central through its link, Home Assistant through the Pi's read-only door. The session connects
forever on its own thread and attaches at every connect, so after a bus restart (which starts empty)
the line, `birth` and `base` are back in the new epoch with no help from Central. HostCore never
waits on the bus: `start` returns at once.

The session serves the host's one method, `reboot` (contracts/node_state.py HOST_REBOOT_METHOD,
decision 0034): Central asks it over the Pi's fleet-pipe link when an Install restarts the Pi. It
answers `HostRebootAnswer(accepted=True)` and only then hands the reason to the host's reboot
effect, which HostCore runs on its own thread (appliance/host/host_runner.py); an ask that does not
decode is answered as an error and reboots nothing.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Final

from contracts.node_link import NODE_BUS_URL, STORE_LINES
from contracts.node_state import (
    HOST_BASE_BYTES,
    HOST_BASE_KEY,
    HOST_LINE,
    HOST_REBOOT_METHOD,
    HostBase,
    HostRebootAnswer,
    decode_host_reboot,
    encode_host_base,
    encode_host_reboot_answer,
)
from contracts.room_light import LIGHT_SENSOR_BYTES, LIGHT_SENSOR_KEY
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.envelope import MethodCall, MethodHandler
from nodeapi.node import NodeSession, Release

HOST_VERSION: Final = "1.0.0"                     # the host component's release version (birth)
# The host line's state table beside birth and outbox.
HOST_STATE: Final = KeyTable({HOST_BASE_KEY: HOST_BASE_BYTES, LIGHT_SENSOR_KEY: LIGHT_SENSOR_BYTES})


def _host_slice() -> Slice:
    """The record buffer takes every byte of the host line the state bucket leaves."""
    state = state_bucket(HOST_LINE, HOST_STATE)
    records = event_buffer(HOST_LINE, "record", STORE_LINES[HOST_LINE].max_bytes - state.max_bytes)
    return Slice(HOST_LINE, (records, state))


HOST_SLICE: Final[Slice] = _host_slice()


_ACCEPTED: Final = encode_host_reboot_answer(HostRebootAnswer(accepted=True))


def _reboot_method(reboot: Callable[[str], None]) -> MethodHandler:
    """`reboot`: decode the ask (ValueError, answered as an error, for one that does not decode),
    answer it accepted, and call `reboot(reason)` once the answer is on its way (the next turn of
    the session's loop, after the reply was handed to the client)."""
    async def answer(call: MethodCall) -> bytes:
        reason = decode_host_reboot(call.payload).reason
        asyncio.get_running_loop().call_soon(reboot, reason)
        return _ACCEPTED
    return answer


def host_session(base_tag: str, *, url: str = NODE_BUS_URL, reboot: Callable[[str], None]) -> NodeSession:
    """The host component's session, not started: it declares the host line, writes birth
    (Release(HOST_VERSION, digest=base_tag, schema_majors={"host": 1})) at every attach, holds
    `base` = encode_host_base(HostBase(base_tag)), and serves `reboot`, which answers accepted and
    then calls `reboot(reason)` on the session's thread (HostCore's hands it to its own thread). Its
    start() returns at once and it connects forever, so HostCore never waits on the bus."""
    session = NodeSession(HOST_LINE, HOST_SLICE, Release(HOST_VERSION, base_tag, {HOST_LINE: 1}), url=url,
                          methods={HOST_REBOOT_METHOD: _reboot_method(reboot)})
    session.state.put(HOST_BASE_KEY, encode_host_base(HostBase(base_tag=base_tag)))
    return session
