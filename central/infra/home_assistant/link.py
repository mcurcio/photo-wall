"""The Home Assistant link: the media worker's one MQTT client of the house's broker. Central's
worker only; a Pi never talks to Home Assistant.

Home Assistant runs the shipped automation (integrations/home-assistant/), which publishes each
chosen light's and illuminance sensor's full state, retained, under the topics `topics` names. The
link subscribes to `topics.SUBSCRIPTION` at QoS 1 with a clean session, so every (re)connect is
handed every retained state again; a repeat records nothing new (`RoomLightReports.record_*`
answers "unchanged"). Each message goes topic -> entity (`topics.topic_of`) -> state or reading
(`messages`) -> record (a recorded change wakes the WALL writer through the store's NOTIFY at
commit, as every writer's does); a refused one goes to the one refusal log.

The link beats its health every HEARTBEAT_SECONDS ("not-configured" without a broker, "connecting"
at start and between attempts, "connected" while subscribed); a reader judges the beat by its age
(`health`). It reconnects forever with backoff RECONNECT_FIRST_SECONDS doubling to
RECONNECT_MAX_SECONDS, after a broker error or a database failure while recording (the retained
states replay on the next connection); neither ends the task. When it ends, it closes its database
pool (`close`).

The broker comes from `PHOTO_WALL_MQTT_URL` (`mqtt://user:password@host:1883` or `mqtts://...`), set
in the deployment from the secret store.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote, urlsplit

import aiomqtt

from central.infra.home_assistant.health import HEARTBEAT_SECONDS, Beat, PgLinkHealth
from central.infra.home_assistant.messages import MessageRefused, illuminance, light_state
from central.infra.home_assistant.topics import (
    SOURCE,
    SUBSCRIPTION,
    illuminance_sensor,
    light_reporter,
    topic_of,
)
from central.room_light.model import ReportRefusal
from central.room_light.ports import RoomLightReports

log = logging.getLogger(__name__)

MQTT_URL_ENV: Final = "PHOTO_WALL_MQTT_URL"
CLIENT_ID_PREFIX: Final = "photo-wall-central"   # + a per-link suffix: two Centrals never evict each other
RECONNECT_FIRST_SECONDS: Final = 1.0
RECONNECT_MAX_SECONDS: Final = 30.0


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
    """The worker's one subscriber of the house's broker (None: no broker configured)."""

    def __init__(self, broker: Broker | None, reports: RoomLightReports, health: PgLinkHealth, *,
                 close: Callable[[], None]) -> None:
        self._broker = broker
        self._reports = reports
        self._health = health
        self._close = close
        self._beat: Beat = "not-configured" if broker is None else "connecting"
        # A clean session needs no stable id; a fixed one would make two Centrals on one broker (a
        # deploy's overlapping workers, a local launch) evict each other in a takeover loop.
        self.client_id = f"{CLIENT_ID_PREFIX}-{uuid.uuid4().hex[:12]}"

    async def run(self, stop: asyncio.Event) -> None:
        """Until `stop`: beat every HEARTBEAT_SECONDS and, with a broker, connect, subscribe and record
        every message, reconnecting with backoff after any failure; then close the link's pool."""
        beating = asyncio.create_task(self._beating(stop), name="home assistant heartbeat")
        try:
            if self._broker is None:
                await stop.wait()
            else:
                await self._connecting(stop)
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating
            await asyncio.to_thread(self._close)

    async def _beating(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._say(self._beat)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), HEARTBEAT_SECONDS)

    async def _say(self, beat: Beat) -> None:
        """Beat `beat` now; a database that is away is logged, and the next beat tries again."""
        self._beat = beat
        try:
            await asyncio.to_thread(self._health.beat, beat)
        except Exception as error:
            log.warning("home assistant link: heartbeat not recorded: %s", error)

    async def _connecting(self, stop: asyncio.Event) -> None:
        delay = RECONNECT_FIRST_SECONDS
        while not stop.is_set():
            await self._say("connecting")
            try:
                async with self._client() as client:
                    await client.subscribe(SUBSCRIPTION, qos=1)
                    await self._say("connected")
                    delay = RECONNECT_FIRST_SECONDS
                    await self._receive(client, stop)
            except aiomqtt.MqttError as error:
                log.info("home assistant link: %s", error)
            except Exception as error:   # a recording that failed: disconnect; the retained states replay
                log.warning("home assistant link: a message was not recorded: %s", error)
            if stop.is_set():
                return
            await self._say("connecting")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    def _client(self) -> aiomqtt.Client:
        broker = self._broker
        assert broker is not None
        return aiomqtt.Client(broker.host, broker.port, username=broker.username, password=broker.password,
                              identifier=self.client_id, clean_session=True,
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
        """One message: record it; a topic or payload this adapter cannot read goes to the refusal
        log. A database failure is raised."""
        named = topic_of(topic)
        if named is None:
            self._reports.record_refused(SOURCE, topic, ReportRefusal.UNKNOWN_ADDRESS)
            return
        try:
            if named.kind == "light":
                self._reports.record_light_state(light_reporter(named.entity), light_state(payload))
            else:
                self._reports.record_reading(illuminance_sensor(named.entity), illuminance(payload))
        except MessageRefused as refused:   # only the payload's reading raises it
            self._reports.record_refused(SOURCE, topic, refused.reason)


def build_home_assistant_link(env: Mapping[str, str], reports: RoomLightReports, health: PgLinkHealth, *,
                              close: Callable[[], None]) -> HomeAssistantLink:
    """The worker's link, always: without MQTT_URL_ENV its run only beats "not-configured"."""
    url = env.get(MQTT_URL_ENV)
    return HomeAssistantLink(broker_from(url) if url else None, reports, health, close=close)


__all__ = ["CLIENT_ID_PREFIX", "MQTT_URL_ENV", "RECONNECT_FIRST_SECONDS", "RECONNECT_MAX_SECONDS", "Broker",
           "HomeAssistantLink", "broker_from", "build_home_assistant_link"]
