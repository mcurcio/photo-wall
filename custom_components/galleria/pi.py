"""A Pi, read through its read-only door (contract.py NODE_DOOR_PORT): one connection per read, no
credentials, one STREAM.MSG.GET per key, no consumers. No Home Assistant import.

A read first checks it reached the Pi it meant: the door's INFO names the bus's server name, which must
be `bus_server_name(serial)`; an address that moved reads "pi_unreachable", never another Pi's values.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import nats
from nats.errors import Error as NatsError
from nats.js import JetStreamContext
from nats.js.errors import NotFoundError

from .const import PI_TIMEOUT
from .contract import (
    BIRTH_KEY,
    FRAMES_KEY,
    HOST_BASE_KEY,
    HOST_STATE_PREFIX,
    HOST_STATE_STREAM,
    KV_OPERATION_HEADER,
    LIGHT_SENSOR_KEY,
    NODE_DOOR_PORT,
    PLAYER_COMPONENT,
    PLAYER_SCHEMA_MAJOR,
    PLAYER_STATE_PREFIX,
    PLAYER_STATE_STREAM,
    Birth,
    FrameReport,
    LightSensorReading,
    bus_server_name,
    decode_birth,
    decode_frame_reports,
    decode_host_base,
    decode_light_sensor,
)

_LOGGER = logging.getLogger(__name__)
_FAILURES = (NatsError, OSError, asyncio.TimeoutError)

# pi_unreachable: every entity of the Pi reads unavailable; not_yet_written: a fresh bus epoch whose
# writer has not refilled it; unreadable: a value the contract refuses, or a Player line of another
# schema major (a repair issue).
ReadError = Literal["pi_unreachable", "not_yet_written", "unreadable"]
READ_ERRORS: frozenset[str] = frozenset({"pi_unreachable", "not_yet_written", "unreadable"})


@dataclass(frozen=True)
class PiReading:
    """One read of a Pi: its base's tag (the release it booted), its light meter (None: none wired),
    its Player's birth and the Frames its Player reports."""
    base_tag: str | ReadError
    light_meter: LightSensorReading | None | ReadError
    player_birth: Birth | ReadError
    frames: tuple[FrameReport, ...] | ReadError


async def _quiet(error: Exception) -> None:
    _LOGGER.debug("a Pi's door: %r", error)


async def _get(jetstream: JetStreamContext, stream: str, subject: str, decode: Callable[[bytes], Any]) -> Any:
    """The decoded value of `subject`'s last message, "not_yet_written" or "unreadable"."""
    try:
        message = await jetstream.get_last_msg(stream, subject)
    except NotFoundError:
        return "not_yet_written"
    if (message.headers or {}).get(KV_OPERATION_HEADER):
        return "not_yet_written"
    try:
        return decode(message.data or b"")
    except ValueError:
        return "unreadable"


async def read_pi(serial: str, address: str, *,
                  port: int = NODE_DOOR_PORT) -> PiReading | Literal["pi_unreachable"]:
    """Read the Pi `serial` at `address` (`port`: the door's; tests run several on one machine)."""
    host = f"[{address}]" if ":" in address else address
    try:
        async with asyncio.timeout(2 * PI_TIMEOUT):   # one try, never nats-py's retry loop
            client = await nats.connect(f"ws://{host}:{port}", connect_timeout=PI_TIMEOUT, allow_reconnect=False,
                                        max_reconnect_attempts=1, reconnect_time_wait=0, error_cb=_quiet,
                                        name="galleria")
    except (*_FAILURES, ValueError) as error:
        _LOGGER.debug("Pi %s at %s: %r", serial, address, error)
        return "pi_unreachable"
    try:
        # nats-py keeps the server's INFO only privately; its server_name is the one check that the
        # address still is this Pi.
        if client._server_info.get("server_name") != bus_server_name(serial):
            return "pi_unreachable"
        jetstream = client.jetstream(timeout=PI_TIMEOUT)
        base = await _get(jetstream, HOST_STATE_STREAM, HOST_STATE_PREFIX + HOST_BASE_KEY, decode_host_base)
        meter = await _get(jetstream, HOST_STATE_STREAM, HOST_STATE_PREFIX + LIGHT_SENSOR_KEY,
                           decode_light_sensor)
        birth = await _get(jetstream, PLAYER_STATE_STREAM, PLAYER_STATE_PREFIX + BIRTH_KEY, decode_birth)
        if not isinstance(birth, str) and birth["schema_majors"].get(PLAYER_COMPONENT) != PLAYER_SCHEMA_MAJOR:
            birth = "unreadable"
        if isinstance(birth, str):
            frames: Any = birth   # no birth of this major: its line's keys are not this contract's to read
        else:
            reports = await _get(jetstream, PLAYER_STATE_STREAM, PLAYER_STATE_PREFIX + FRAMES_KEY,
                                 decode_frame_reports)
            frames = reports if isinstance(reports, str) else tuple(reports["frames"])
    except _FAILURES as error:
        _LOGGER.debug("Pi %s at %s: %r", serial, address, error)
        return "pi_unreachable"
    finally:
        try:
            await client.close()
        except _FAILURES:
            pass
    return PiReading(
        base_tag=base if isinstance(base, str) else base["base_tag"],
        light_meter=None if meter == "not_yet_written" else meter,   # a Pi with no meter writes no key
        player_birth=birth,
        frames=frames,
    )
