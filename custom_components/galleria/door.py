"""Central's door: Galleria's one connection to Central's hub (contract.py DOOR_PATH), relayed through
Central's origin to the hub's WebSocket listener, as HOME_ASSISTANT_USER. The hub holds the user's
permissions: publish Home Assistant's states, read Central's state one key at a time, call Central's
methods, nothing else. No Home Assistant import: this is the transport, the entities are elsewhere.

nats-py keeps the connection and reconnects forever; every failure of a call is DoorUnavailable."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from nats.aio.client import Client
from nats.errors import Error as NatsError
from nats.js.errors import NotFoundError

from .const import CENTRAL_TIMEOUT, INSTALL_TIMEOUT
from .contract import (
    CENTRAL_DOOR_API_PREFIX,
    CENTRAL_STATE_PREFIX,
    CENTRAL_STATE_STREAM,
    DOOR_PATH,
    GALLERIA_BIRTH_KEY,
    HOME_ASSISTANT_STATES_PREFIX,
    HOME_ASSISTANT_USER,
    INSTALL_PLAYER_SUBJECT,
    KV_OPERATION_HEADER,
    GalleriaBirth,
    HomeAssistantState,
    InstallRefused,
    InstallStarted,
    decode_install_answer,
    encode_galleria_birth,
    encode_home_assistant_state,
    encode_install_player,
)

_LOGGER = logging.getLogger(__name__)
_SERVICE_ERROR_HEADER = "Nats-Service-Error"   # nats.micro's error reply (a handler that raised)
_FAILURES = (NatsError, OSError, asyncio.TimeoutError)


class DoorUnavailable(Exception):
    """Central's door did not answer as it should (not reachable, no bucket yet, an error reply)."""


def normalized_origin(origin: str) -> str:
    """`scheme://host[:port]` of `origin` (http when it names none; lower-case; no path): the entry's
    unique id. ValueError for anything but an http(s) origin with a host."""
    text = origin.strip()
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    port = parts.port   # ValueError for a port that is not one
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.path.strip("/"):
        raise ValueError("origin")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme.lower()}://{host}{f':{port}' if port else ''}"


def door_url(origin: str) -> str:
    """`ws(s)://<origin's host:port>/` + DOOR_PATH: ws for an http origin, wss for https."""
    scheme, rest = normalized_origin(origin).split("://", 1)
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/{DOOR_PATH}"


async def _quiet(error: Exception) -> None:
    _LOGGER.debug("Central's door: %r", error)


class CentralDoor:
    """One connection to Central's hub through its door."""

    def __init__(self, client: Client) -> None:
        self._client = client
        # Central's state read is imported into the door's own account under its own API prefix.
        self._jetstream = client.jetstream(prefix=CENTRAL_DOOR_API_PREFIX, timeout=CENTRAL_TIMEOUT)

    @classmethod
    async def connect(cls, origin: str) -> CentralDoor:
        """Connected to `origin`'s door; DoorUnavailable when it does not answer now."""
        client = Client()
        try:
            # nats-py retries a first connect forever when it may reconnect forever: the first one
            # answers now or not at all, and a client left mid-connect is closed.
            async with asyncio.timeout(2 * CENTRAL_TIMEOUT):
                await client.connect(
                    door_url(origin), user=HOME_ASSISTANT_USER, password=HOME_ASSISTANT_USER, name="galleria",
                    connect_timeout=CENTRAL_TIMEOUT, allow_reconnect=True, max_reconnect_attempts=-1,
                    reconnect_time_wait=2, error_cb=_quiet)
        except (*_FAILURES, ValueError) as error:
            await cls(client).close()
            raise DoorUnavailable(str(error) or type(error).__name__) from error
        return cls(client)

    @property
    def connected(self) -> bool:
        return self._client.is_connected

    async def read(self, key: str) -> bytes | None:
        """Central's state `key` (one STREAM.MSG.GET by last subject); None: not yet written."""
        try:
            message = await self._jetstream.get_last_msg(CENTRAL_STATE_STREAM, CENTRAL_STATE_PREFIX + key)
        except NotFoundError:
            return None
        except _FAILURES as error:
            raise DoorUnavailable(f"read {key}: {error!r}") from error
        if (message.headers or {}).get(KV_OPERATION_HEADER):
            return None
        return message.data or b""

    async def publish(self, entity_id: str, state: HomeAssistantState) -> None:
        """Put `state` as `entity_id`'s in Home Assistant's states (a JetStream publish, acknowledged);
        DoorUnavailable when the hub does not take it (no bucket yet included); ValueError for a state
        the contract refuses."""
        payload = encode_home_assistant_state(state)
        try:
            await self._jetstream.publish(HOME_ASSISTANT_STATES_PREFIX + entity_id, payload)
        except _FAILURES as error:
            raise DoorUnavailable(f"publish {entity_id}: {error!r}") from error

    async def publish_birth(self, birth: GalleriaBirth) -> None:
        """Put `birth` under GALLERIA_BIRTH_KEY in Home Assistant's states (acknowledged);
        DoorUnavailable when the hub does not take it; ValueError for a birth the contract refuses."""
        payload = encode_galleria_birth(birth)
        try:
            await self._jetstream.publish(HOME_ASSISTANT_STATES_PREFIX + GALLERIA_BIRTH_KEY, payload)
        except _FAILURES as error:
            raise DoorUnavailable(f"publish {GALLERIA_BIRTH_KEY}: {error!r}") from error

    async def install_player(self, serial: str, version: str) -> InstallStarted | InstallRefused:
        """Central's installPlayer(serial, version), answered once within INSTALL_TIMEOUT."""
        payload = encode_install_player({"serial": serial, "version": version})
        try:
            reply = await self._client.request(INSTALL_PLAYER_SUBJECT, payload, timeout=INSTALL_TIMEOUT)
        except _FAILURES as error:
            raise DoorUnavailable(f"installPlayer: {error!r}") from error
        failure = (reply.headers or {}).get(_SERVICE_ERROR_HEADER)
        if failure:
            raise DoorUnavailable(f"installPlayer: {failure}")
        try:
            return decode_install_answer(reply.data)
        except ValueError as error:
            raise DoorUnavailable("installPlayer: an answer the contract refuses") from error

    async def close(self) -> None:
        try:
            await self._client.close()
        except _FAILURES as error:
            _LOGGER.debug("closing Central's door: %r", error)
