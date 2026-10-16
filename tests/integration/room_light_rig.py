"""The room-light tracer's rig: the house's broker, the worker as `media.worker` composes it, the
Player as its `main()` starts it (`PlayerService.start`) and a Pi's light sensor on disk.

Everything Node-bus (servers, ports, `until`) is bus_servers.py's; this module adds only what the
room-light tracer needs beside it.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

import aiomqtt
import httpx
from integration.bus_servers import _free_port, nats_server_binary, until
from integration.test_central_hub import RESTART_SECONDS
from integration.test_central_worker_hub import _IdleRuntime
from test_player_service import TRUST, Server, boot_context, close, immediate
from uplink_fakes import finding

from contracts.room_light import RoomLightsDocument
from contracts.time import ManualClock
from player.identity import load_identity
from player.luts import PlayerLuts
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService

__all__ = ["RESTART_SECONDS", "Broker", "Player", "Worker", "light_sensor_root"]


class Broker:
    """The pinned nats-server as the house's MQTT broker: its MQTT listener needs JetStream, which
    keeps retained messages as Home Assistant's Mosquitto does."""

    def __init__(self, tmp: Path) -> None:
        self.port = _free_port()
        directory = tmp / "broker"
        directory.mkdir()
        self.config = directory / "broker.conf"
        self.config.write_text(f'server_name: house\nlisten: 127.0.0.1:{_free_port()}\n'
                               f'jetstream {{ store_dir: "{directory / "js"}", max_memory_store: 16MB, '
                               'max_file_store: 64MB }\n'
                               f'mqtt {{ listen: 127.0.0.1:{self.port} }}\n')
        self.log = directory / "broker.log"
        self._process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        """The worker's PHOTO_WALL_MQTT_URL for this broker."""
        return f"mqtt://127.0.0.1:{self.port}"

    def start(self) -> None:
        with self.log.open("ab") as log:
            self._process = subprocess.Popen([str(nats_server_binary()), "-c", str(self.config)],
                                             stdout=log, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(10)

    async def publish(self, topic: str, payload: bytes) -> None:
        """Publish `payload` retained at QoS 1, as Home Assistant's automation does; retried until
        the broker takes it."""
        async def taken():
            try:
                async with aiomqtt.Client("127.0.0.1", self.port, identifier="home-assistant") as client:
                    await client.publish(topic, payload, qos=1, retain=True)
                return True
            except aiomqtt.MqttError:
                return False
        await until(taken, 10, f"the broker takes {topic}")


class Worker:
    """`media.worker`'s own loops, composed by its own `build_bus_and_link`, under `_run_workers`
    with a media loop and a job runtime that only wait; stopped as the process is, by SIGTERM."""

    def __init__(self, dsn: str, env: Mapping[str, str]) -> None:
        from media.worker import build_bus_and_link
        self._parts = build_bus_and_link(dsn, env)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        from media.worker import _run_workers
        self._task = asyncio.create_task(_run_workers(asyncio.Event().wait(), _IdleRuntime(), *self._parts))

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():   # a worker that ended has removed its handler: SIGTERM would kill pytest
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(self._task, 30)


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
