"""The Home Assistant tracers' rig: Central's app under uvicorn (its door relayed to the hub), a reader
at a Pi's read-only door, and room_light_rig's Player bound to the Frames Central binds.

Servers, ports, `until` and the door's get are bus_servers.py's; Home Assistant at Central's door,
the worker and the Player are room_light_rig.py's. What is here is only what both tracers share
beside them. Every client speaks NATS as Home Assistant does (nats-py over WebSocket) and holds no
credential the doors do not name.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import nats
import nats.errors
import uvicorn
from integration.bus_servers import BusServer, _free_port, door, door_get, until
from integration.room_light_rig import HomeAssistant, Player, Worker
from test_player_service import TRUST, Server, boot_context
from test_registry import ADMIN
from uplink_fakes import finding

from central.app import create_app
from central.db import Database
from central.fleet.node_sessions import NodeControlConfig
from central.node_bus_wiring import HUB_CONFIG_ENV, HUB_URL_ENV
from contracts.central_api import DOOR_PATH, CentralStateValue, decode_central_state
from contracts.models import OutputBinding, PlayerConfiguration
from contracts.node_link import bus_server_name
from contracts.time import Clock, ManualClock
from player.identity import load_identity
from player.luts import PlayerLuts
from player.mainloop import MainLoopDispatcher
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService

ADMIN_HEADERS = {"Authorization": "Bearer " + ADMIN}
START_SECONDS = 30.0
REFUSAL_SECONDS = 5.0
TICK_SECONDS = 0.033   # the Player's GLib tick period (player/mainloop.py: a 33 ms tick)


class CentralOrigin:
    """Central's HTTP process: `create_app` (on `clock`, the registry's) under uvicorn on one loopback
    port, in its own thread and event loop, with the hub's leaf URL, from which Central derives its
    door. `call` is an operator's (or a booting Pi's) request to its routes."""

    def __init__(self, database: Database, clock: Clock, hub: BusServer, *, cache_root: Path | None = None) -> None:
        self.port = _free_port()
        self._app = create_app(database, clock, ADMIN, run_scheduler=False, cache_root=cache_root,
                               node_control=NodeControlConfig("node-test"),
                               hub_leaf_url=f"ws://127.0.0.1:{hub.websocket_port}/leafnode")
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=10)

    @property
    def door_url(self) -> str:
        """Where Home Assistant dials: Central's origin at "/" + DOOR_PATH, over ws."""
        return f"ws://127.0.0.1:{self.port}/{DOOR_PATH}"

    def start(self) -> None:
        self._server = uvicorn.Server(uvicorn.Config(self._app, host="127.0.0.1", port=self.port,
                                                     log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + START_SECONDS
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"Central did not start on port {self.port}")
            time.sleep(.05)

    def call(self, method: str, path: str, *, json: object = None, content: bytes | None = None,
             headers: Mapping[str, str] = ADMIN_HEADERS) -> httpx.Response:
        """One request; an answer of 300 or more fails the test with its body."""
        answer = self._http.request(method, path, json=json, content=content, headers=dict(headers))
        assert answer.status_code < 300, (method, path, answer.status_code, answer.text)
        return answer

    def stop(self) -> None:
        self._http.close()
        if self._server is None:
            return
        self._server.should_exit = True
        self._thread.join(START_SECONDS)
        assert not self._thread.is_alive(), "Central did not stop"
        self._server = self._thread = None


def worker(dsn: str, hub: BusServer, tmp: Path) -> Worker:
    """room_light_rig's Worker (`media.worker`'s loops, its Node bus from `build_node_bus`) on the
    environment the worker reads: the hub's client URL and its configuration file."""
    return Worker(dsn, {HUB_URL_ENV: hub.client_url, HUB_CONFIG_ENV: str(tmp / "worker-hub.json")})


async def central_state(house: HomeAssistant, key: str) -> CentralStateValue | None:
    """Central's state `key` read at the door, decoded strictly; None until Central has put it."""
    raw = await house.central(key)
    return None if raw is None else decode_central_state(key, raw)


async def refused(client: nats.NATS, violations: list[str], subject: str, payload: bytes) -> None:
    """Publish `payload` on `subject` and wait until the server names it a permissions violation."""
    await client.publish(subject, payload)
    await client.flush()

    async def violation() -> bool:
        return any("permissions violation" in error.lower() and f'publish to "{subject.lower()}"' in error.lower()
                   for error in violations)
    await until(violation, REFUSAL_SECONDS, f"the server refuses a publish to {subject}")


class PiReader:
    """A home-network reader at a Pi's read-only door (bus_servers `door`: no credentials), checked to
    have reached that Pi by the server name its INFO carries; every error the server sends it (a
    permissions violation arrives that way) lands in `violations`."""

    def __init__(self, client: nats.NATS, violations: list[str]) -> None:
        self.client = client
        self.violations = violations

    @classmethod
    async def at(cls, node: BusServer, serial: str, seconds: float) -> PiReader:
        violations: list[str] = []

        async def errors(error: Exception) -> None:
            violations.append(str(error))

        async def answered() -> nats.NATS | None:
            try:
                return await door(node, error_cb=errors)
            except (OSError, TimeoutError, nats.errors.Error):
                return None
        client = await until(answered, seconds, f"a reader reaches {serial}'s door")
        name = client._server_info.get("server_name")   # nats-py keeps the INFO's server name only here
        assert name == bus_server_name(serial), (name, serial)
        return cls(client, violations)

    async def get(self, stream: str, subject: str) -> bytes | None:
        """The latest stored message on `subject` in `stream` (one STREAM.MSG.GET); None: none."""
        return await door_get(self.client, stream, subject)

    async def close(self) -> None:
        await self.client.close()


class BoundServer(Server):
    """The Player tests' fake Central (test_player_service.py) whose control State carries `bindings`,
    Central's own for this Player (its Frames, generations and lighting, as Central's control route
    served them), under the identity the fake enrolled the Player with."""

    def __init__(self, clock: ManualClock, bindings: Sequence[OutputBinding]) -> None:
        super().__init__(clock)
        self.bindings = tuple(bindings)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/player/state" and self.state is None:
            self.offer(bindings=self.bindings, layers=())
        return super().__call__(request)


class _IdleLoop:
    """The GLib idle queue `MainLoopDispatcher` posts to (`idle_add`, from any thread), run by
    `iterate` on the Player's own thread as GLib's main loop runs it."""

    def __init__(self) -> None:
        self._ready: collections.deque = collections.deque()

    def idle_add(self, callback, *, priority):
        self._ready.append(callback)
        return len(self._ready)

    def iterate(self) -> None:
        for _ in range(len(self._ready)):
            callback = self._ready.popleft()
            if callback():
                self._ready.append(callback)


class BoundPlayer(Player):
    """room_light_rig's Player (PlayerService.start on the Node's bus, a RecordingRenderer), its control
    State carrying `bindings`, on a main loop of its own: built on the calling thread (its GLib thread),
    whose event loop runs the Player's dispatcher (the production `MainLoopDispatcher`) and its tick
    every TICK_SECONDS, as the Player's `main()` runs them on GLib."""

    def __init__(self, tmp: Path, node_url: str, bindings: Sequence[OutputBinding]) -> None:
        cache = tmp / "cache"
        cache.mkdir(mode=0o700, parents=True)
        clock = ManualClock(100)
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(BoundServer(clock, bindings)),
                                         trust_env=False)
        self.renderer = RecordingRenderer()
        self._idle = _IdleLoop()
        self._loop: asyncio.Task | None = None
        self.service = PlayerService(
            PlayerConfig(central_origin="http://central", cache_dir=str(cache), cache_bytes=1024**2),
            load_identity(), (), self.renderer, MainLoopDispatcher(self._idle), find_central=finding("http://central"),
            trust=TRUST, clock=clock, client=self._client, time_client=self._client, websocket_connect=False,
            boot_context=boot_context(), luts=PlayerLuts(), node_bus_url=node_url)

    def start(self) -> None:
        super().start()
        self._loop = asyncio.create_task(self._main_loop(), name="bound player main loop")

    async def _main_loop(self) -> None:
        while True:
            self._idle.iterate()
            if not self.service.stopped:
                self.service.tick_main()
            await asyncio.sleep(TICK_SECONDS)

    async def stop(self) -> None:
        await super().stop()   # the loop keeps running dispatched callbacks while the Player stops
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop


def central_bindings(central: CentralOrigin, player: Mapping[str, object]) -> tuple[OutputBinding, ...]:
    """The bindings Central's control route serves the enrolled `player` (after its hello)."""
    answer = central.call("GET", "/v1/player/state", headers={"Authorization": f"Bearer {player['token']}"})
    return PlayerConfiguration.model_validate(answer.json()["configuration"]).bindings


__all__ = [
    "ADMIN_HEADERS", "BoundPlayer", "CentralOrigin", "PiReader", "central_bindings", "central_state", "refused",
    "worker",
]
