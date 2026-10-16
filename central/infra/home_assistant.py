"""The Home Assistant link (decision 0028; owner, 2026-10-15: "i want home assistant to trigger "the
spotlight turned on" or "the overhead lights turned off" and leave it to the node renderer"): the
media worker's one MQTT client of the house's broker. Central's worker only; a Pi never talks to
Home Assistant (pyproject's "Only Central talks MQTT").

Home Assistant runs the shipped automation (integrations/home-assistant/), which publishes each
chosen light's and illuminance sensor's full state, retained, to `photowall/light/<entity>` and
`photowall/illuminance/<entity>`. The link subscribes to `SUBSCRIPTION` at QoS 1 with a clean
session, so every (re)connect is handed every retained state again; a repeat records nothing new
(`RoomLightReports.record_*` answers "unchanged"). Each message goes topic -> entity
(`policy.topic_entity`) -> parse -> record; a "recorded" answer wakes the WALL writer at once
(`on_recorded`), a refused one is kept (`record_refusal`). The link reconnects forever with backoff
RECONNECT_FIRST_SECONDS doubling to RECONNECT_MAX_SECONDS, and records "connected" and "away" on each
transition.

The broker comes from `PHOTO_WALL_MQTT_URL` (`mqtt://user:password@host:1883` or `mqtts://...`), set in
the deployment from the secret store; unset, the worker runs no link and records "not-configured".
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote, urlsplit

import aiomqtt

from central.room_light.model import SUBSCRIPTION, Refused
from central.room_light.policy import parse_illuminance_message, parse_light_message, topic_entity
from central.room_light.ports import RoomLightReports

log = logging.getLogger(__name__)

MQTT_URL_ENV: Final = "PHOTO_WALL_MQTT_URL"
CLIENT_ID: Final = "photo-wall-central"
RECONNECT_FIRST_SECONDS: Final = 1.0
RECONNECT_MAX_SECONDS: Final = 30.0
_OUT_OF_RANGE: Final = "out-of-range"


@dataclass(frozen=True)
class Broker:
    """Where the house's broker is, from PHOTO_WALL_MQTT_URL."""
    host: str
    port: int
    username: str | None
    password: str | None
    tls: bool


def broker_from(url: str) -> Broker:
    """The broker an `mqtt://` or `mqtts://` URL names (default ports 1883 and 8883). ValueError for
    anything else."""
    parts = urlsplit(url)
    if parts.scheme not in ("mqtt", "mqtts") or not parts.hostname:
        raise ValueError("mqtt_url")
    tls = parts.scheme == "mqtts"
    return Broker(parts.hostname, parts.port or (8883 if tls else 1883),
                  None if parts.username is None else unquote(parts.username),
                  None if parts.password is None else unquote(parts.password), tls)


class HomeAssistantLink:
    """The worker's one subscriber of the house's broker."""

    def __init__(self, broker: Broker, reports: RoomLightReports, on_recorded: Callable[[], None]) -> None:
        self._broker = broker
        self._reports = reports
        self._on_recorded = on_recorded

    async def run(self, stop: asyncio.Event) -> None:
        """Until `stop`: connect, subscribe, record every message; reconnect with backoff on any
        broker error. Records "away" when it stops after a connection."""
        delay = RECONNECT_FIRST_SECONDS
        while not stop.is_set():
            connected = False
            try:
                async with self._client() as client:
                    await client.subscribe(SUBSCRIPTION, qos=1)
                    await asyncio.to_thread(self._reports.record_link, "connected")
                    connected = True
                    delay = RECONNECT_FIRST_SECONDS
                    await self._receive(client, stop)
            except aiomqtt.MqttError as error:
                log.info("home assistant link: %s", error)
            if connected:
                await asyncio.to_thread(self._reports.record_link, "away")
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    def _client(self) -> aiomqtt.Client:
        broker = self._broker
        return aiomqtt.Client(broker.host, broker.port, username=broker.username, password=broker.password,
                              identifier=CLIENT_ID, clean_session=True,
                              tls_context=ssl.create_default_context() if broker.tls else None)

    async def _receive(self, client: aiomqtt.Client, stop: asyncio.Event) -> None:
        messages = client.messages.__aiter__()
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while True:
                nxt = asyncio.ensure_future(messages.__anext__())
                done, _ = await asyncio.wait((nxt, stopping), return_when=asyncio.FIRST_COMPLETED)
                if nxt not in done:
                    nxt.cancel()
                    return
                message = nxt.result()
                payload = message.payload if isinstance(message.payload, bytes | bytearray) else b""
                await asyncio.to_thread(self.record, message.topic.value, bytes(payload))
        finally:
            stopping.cancel()

    def record(self, topic: str, payload: bytes) -> None:
        """One message: record it, wake the WALL writer when it changed the document, keep a refusal."""
        entity = topic_entity(topic)
        if entity is None:
            self._reports.record_refusal(topic, _OUT_OF_RANGE)
            return
        kind, name = entity
        try:
            answer = (self._reports.record_light_state(name, parse_light_message(payload)) if kind == "light"
                      else self._reports.record_illuminance(name, parse_illuminance_message(payload)))
        except ValueError as refused:
            self._reports.record_refusal(topic, str(refused) or _OUT_OF_RANGE)
            return
        if isinstance(answer, Refused):
            self._reports.record_refusal(topic, answer.reason.value)
        elif answer == "recorded":
            self._on_recorded()


def build_home_assistant_link(env: Mapping[str, str], reports: RoomLightReports,
                              on_recorded: Callable[[], None]) -> HomeAssistantLink | None:
    """The worker's link, or None without MQTT_URL_ENV (the link is "not-configured")."""
    url = env.get(MQTT_URL_ENV)
    if not url:
        return None
    return HomeAssistantLink(broker_from(url), reports, on_recorded)


__all__ = ["CLIENT_ID", "MQTT_URL_ENV", "RECONNECT_FIRST_SECONDS", "RECONNECT_MAX_SECONDS", "Broker",
           "HomeAssistantLink", "broker_from", "build_home_assistant_link"]
