"""The room-light tracer's rig: Home Assistant on Central's hub (as the Galleria integration speaks to
it), the worker as `media.worker` composes it, the Player as its `main()` starts it
(`PlayerService.start`) and a Pi's light sensor on disk.

Everything Node-bus (servers, ports, `until`) is bus_servers.py's; this module adds only what the
room-light tracer and the door's tests need beside it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
from collections.abc import Mapping
from pathlib import Path

import httpx
import nats
import nats.errors
from integration.bus_servers import until
from integration.test_central_hub import RESTART_SECONDS
from integration.test_central_worker_hub import _IdleRuntime
from test_player_service import TRUST, Server, boot_context, close, immediate
from uplink_fakes import finding

from contracts.central_api import (
    CENTRAL_COMPONENT,
    CENTRAL_DOOR_API_PREFIX,
    CENTRAL_STATE_PREFIX,
    CENTRAL_STATE_STREAM,
    HOME_ASSISTANT_STATES_PREFIX,
    HOME_ASSISTANT_USER,
    INSTALL_PLAYER,
    InstallPlayer,
    InstallRefused,
    InstallStarted,
    decode_install_answer,
    encode_install_player,
)
from contracts.home_assistant import HomeAssistantState, encode_home_assistant_state
from contracts.node_link import METHOD_TOKEN
from contracts.room_light import RoomLightsDocument
from contracts.time import ManualClock
from player.identity import load_identity
from player.luts import PlayerLuts
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService

__all__ = ["RESTART_SECONDS", "HomeAssistant", "Player", "Worker", "light_sensor_root"]


class HomeAssistant:
    """Home Assistant at a door of Central's hub (`url`: the hub's client WebSocket, or Central's
    origin at "/" + DOOR_PATH) as HOME_ASSISTANT_USER, over nats-py's WebSocket transport. Every
    permission violation the hub reports lands in `violations`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.violations: list[str] = []
        self.client: nats.NATS | None = None

    async def connect(self, user: str = HOME_ASSISTANT_USER) -> nats.NATS:
        """A client at the door as `user` (password = user name); nats' error for a refused login."""
        async def errors(error: Exception) -> None:
            self.violations.append(str(error))
        return await nats.connect(servers=[self.url], user=user, password=user, allow_reconnect=False,
                                  connect_timeout=2, error_cb=errors)

    async def __aenter__(self) -> HomeAssistant:
        self.client = await until(self._connected, 10, f"Home Assistant connects at {self.url}")
        return self

    async def _connected(self):
        try:
            return await self.connect()
        except (OSError, nats.errors.Error):
            return None

    async def __aexit__(self, *_exc) -> None:
        if self.client is not None:
            await self.client.close()

    async def publish(self, entity: str, state: HomeAssistantState | bytes) -> None:
        """Put `state` as `entity`'s, as the integration does: a JetStream publish, retried until the hub
        acknowledges it (the bucket exists once Central's hub session declared it)."""
        payload = state if isinstance(state, bytes) else encode_home_assistant_state(state)

        async def taken():
            try:
                await self.client.jetstream().publish(HOME_ASSISTANT_STATES_PREFIX + entity, payload, timeout=2)
                return True
            except (nats.errors.Error, asyncio.TimeoutError):
                return False
        await until(taken, 10, f"the hub takes {entity}'s state")

    async def central(self, key: str) -> bytes | None:
        """Central's state `key`: one STREAM.MSG.GET by last subject (None: not written)."""
        reply = await self.client.request(f"{CENTRAL_DOOR_API_PREFIX}.STREAM.MSG.GET.{CENTRAL_STATE_STREAM}",
                                          json.dumps({"last_by_subj": CENTRAL_STATE_PREFIX + key}).encode(), timeout=2)
        message = json.loads(reply.data).get("message")
        return None if message is None else base64.b64decode(message["data"])

    async def install(self, serial: str, version: str) -> InstallStarted | InstallRefused:
        """installPlayer, answered once."""
        reply = await self.client.request(f"{CENTRAL_COMPONENT}.{METHOD_TOKEN}.{INSTALL_PLAYER}",
                                          encode_install_player(InstallPlayer(serial=serial, version=version)),
                                          timeout=10)
        return decode_install_answer(reply.data)


class Worker:
    """`media.worker`'s own loops, its Node bus composed by `build_node_bus`, under `_run_workers` with
    a media loop and a job runtime that only wait; stopped as the process is, by SIGTERM."""

    def __init__(self, dsn: str, env: Mapping[str, str]) -> None:
        from central.node_bus_wiring import build_node_bus
        self._bus = build_node_bus(dsn, env)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        from media.worker import _run_workers
        self._task = asyncio.create_task(_run_workers(asyncio.Event().wait(), _IdleRuntime(), self._bus))

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():   # a worker that ended has removed its handler: SIGTERM would kill pytest
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(self._task, 30)
        self._task = None


class Player:
    """A PlayerService started as the Player's `main()` starts it (`start`, which also follows the room
    lights from the Node's bus at `node_url`), drawing on a RecordingRenderer; its Central is the
    Player tests' fake (test_player_service.py), since the room lights never cross Central's link.
    Built and started on the calling thread, as `main()` does on the GLib thread."""

    def __init__(self, tmp: Path, node_url: str) -> None:
        cache = tmp / "cache"
        cache.mkdir(mode=0o700, parents=True)
        clock = ManualClock(100)
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(Server(clock)), trust_env=False)
        self.renderer = RecordingRenderer()
        self.service = PlayerService(
            PlayerConfig(central_origin="http://central", cache_dir=str(cache), cache_bytes=1024**2),
            load_identity(), (), self.renderer, immediate, find_central=finding("http://central"), trust=TRUST,
            clock=clock, client=self._client, time_client=self._client, websocket_connect=False,
            boot_context=boot_context(), luts=PlayerLuts(), node_bus_url=node_url)

    def start(self) -> None:
        self.service.start()

    def latest(self) -> RoomLightsDocument | None:
        """The last room-lights document the Player drew its Frames with."""
        drawn = list(self.renderer.room_lights)
        return drawn[-1] if drawn else None

    async def stop(self) -> None:
        self.service.stop()

        async def stopped():
            return self.service.stopped
        await until(stopped, 10, "the Player stops")
        await close(self.service)


def light_sensor_root(tmp: Path, lux: float) -> Path:
    """An IIO devices directory holding one light sensor whose processed channel reads `lux`."""
    root = tmp / "iio"
    device = root / "iio:device0"
    device.mkdir(parents=True)
    (device / "name").write_text("tsl2591\n")
    (device / "in_illuminance_input").write_text(f"{lux}\n")
    return root
