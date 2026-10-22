"""Single-process Player control plane; renderer calls stay on the GLib thread."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import functools
import json
import logging
import math
import queue
import re
import signal
import threading
import uuid
from collections import deque
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import ConfigDict, Field, ValidationError, model_validator

from contracts.clock_record import ClockRecord
from contracts.enrollment import OutputReport
from contracts.equipment import READ_CAP, equipment_device_id
from contracts.liveness import REPORT_INTERVAL, SESSION_BACKOFF
from contracts.lut import LUT_BYTES, LUT_MEDIA_TYPE, LutRef
from contracts.models import (
    MAX_VARIANT_BYTES,
    Commit,
    IdentifyOutput,
    Instant,
    Layer,
    Model,
    Observation,
    Plan,
    PlayerConfiguration,
    PlayerTime,
    Revocation,
)
from contracts.node_link import NODE_BUS_URL
from contracts.node_state import FrameReport, FrameReports
from contracts.player_control import (
    ControlAck,
    ControlAckResponse,
    ControlAppliedReceipt,
    ControlHello,
    ControlSelection,
)
from contracts.room_light import (
    LUX_MAX,
    ROOM_LIGHTS_DRAWN_REPEAT_SECONDS,
    RoomLightsDocument,
    RoomLightsDrawn,
    room_lights_digest,
)
from contracts.time import Clock, SystemClock, TimeMapping
from nodeapi.node import WallReader
from player.cache import Cache, CacheError
from player.central_link import CentralLink, Session, read_refusal
from player.executor import AcquisitionStopped, AuthorityError, Executor
from player.geometry import light_lux_on_frame
from player.identity import Identity, load_identity
from player.luts import LutWants, PlayerLuts
from player.mainloop import DispatchRefused, LatePosts, LoopLate, MainLoopDispatcher, Tick
from player.node_app_link import NodeAppLinkClient
from player.output_discovery import discover_outputs
from player.player_line import PlayerLine
from player.probe_responder import ProbeResponder
from player.renderer_root import start_renderer
from player.rendering import Renderer
from player.room_lights import start_room_lights
from uplink.causes import Cause, UplinkError
from uplink.clock import RunClockRecord
from uplink.diagnosis import failure_text
from uplink.finder import Found, find_central
from uplink.locate import LocatedCentral
from uplink.origin import Origin
from uplink.resolver import (
    KERNEL_COMMAND_LINE,
    Configured,
    Unconfigured,
    read_kernel_command_line,
    resolve_central,
)
from uplink.transport import HttpTransport, Transport
from uplink.trust import Trust

MAX_JSON = 1024 * 1024
CHUNK_SIZE = 64 * 1024
_REPR_DIGEST_SHA256 = re.compile(r"^sha-256=:([A-Za-z0-9+/]{43}=):(?:;.*)?$")


def served_sha256(header: str | None) -> str:
    """The SHA-256 (hex) a media response's `Repr-Digest` (RFC 9530, a structured Dictionary
    of algorithm to byte sequence) names; without one the bytes cannot be checked."""
    for member in (header or "").split(","):
        match = _REPR_DIGEST_SHA256.fullmatch(member.strip())
        if match is not None:
            try:
                return base64.b64decode(match.group(1), validate=True).hex()
            except binascii.Error:
                break
    raise ServiceError("media_digest")
# The session-cycle backoff is a liveness contract (Central's silence threshold is derived from
# it); the name is kept so tests can shorten the session retry.
BACKOFF = SESSION_BACKOFF
# A failed media download retries on its own schedule; it is not a liveness contract.
MEDIA_RETRY_BACKOFF = (1, 5, 15, 60)
# The app link is tried the moment an applied receipt (or a relink) arrives; this is only
# the backoff after a refusal or a socket failure, and the idle safety net.
APP_LINK_RETRY = 5.0
# How long a thread-to-main handoff may wait for the GLib loop before the Player raises the
# main_loop_late fault instead of waiting on a starved loop in silence (G8). Readiness
# is left as last sent: a late loop never withdraws prepared playback (E-G8-8 decision).
DISPATCH_DEADLINE = 2 * REPORT_INTERVAL
# The planning tick's period (player/mainloop.py `Tick`): the executor's draw grace must outlast a
# commit in flight plus one of them (player/executor.py `DRAW_GRACE`).
PLANNING_TICK_MS = 33
LOG = logging.getLogger("photo_wall.player")


class ServiceError(ValueError):
    """A bounded diagnostic code, with no response body or credential attached."""


class Unauthorized(ServiceError):
    pass


class StaleFeedback(ServiceError):
    pass


class MainLoopLate(ServiceError):
    """The GLib loop is late: a dispatch missed DISPATCH_DEADLINE (its callback abandoned
    unless already running), or an earlier abandoned one is still unreached."""


class PlayerConfig(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = Field(default=1, alias="schema")
    # The saved root (R1): written by the node's handoff stage (/etc/photo-wall/public.json),
    # and used only when the kernel command line names no Central. Opaque until saved_root():
    # any JSON value loads (no type or length check here), and only saved_root() -- called
    # only when the cmdline names no Central -- validates it, with the one Central-root
    # validator, uplink.origin.Origin.parse_root. So when the cmdline names Central, no
    # saved value of any type or length can stop the Player (U3).
    central_origin: Any = Field(default=None, repr=False)
    # A PEM bundle used instead of the Debian bundle for every Central request this
    # process makes (locate, httpx, websocket): main() builds the one Trust from it.
    ca_file: str | None = Field(default=None, max_length=4096)
    cache_dir: str | None = Field(default=None, max_length=4096)
    cache_bytes: int = Field(default=512 * 1024**2, ge=1024**2, le=1024**4)

    @model_validator(mode="after")
    def trusted_origin(self):
        if self.cache_dir is not None and not Path(self.cache_dir).is_absolute():
            raise ValueError("absolute cache directory required")
        if self.ca_file is not None and not Path(self.ca_file).is_absolute():
            raise ValueError("absolute public CA path required")
        return self

    def saved_root(self) -> Origin | None:
        """The saved root, or None when no central_origin is configured. Raises
        UplinkError(CONFIGURATION, "invalid") when it is not a Central root, a non-string or
        an over-long value included (Origin.parse_root refuses both)."""
        return None if self.central_origin is None else Origin.parse_root(self.central_origin)


def _json(data: bytes | str) -> dict:
    if len(data.encode() if isinstance(data, str) else data) > MAX_JSON:
        raise ServiceError("body_limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ServiceError("duplicate_field")
            result[key] = value
        return result

    try:
        result = json.loads(data, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError, UnicodeError) as error:
        raise ServiceError("invalid_json") from error
    if not isinstance(result, dict):
        raise ServiceError("invalid_json")
    return result


def load_config(path: Path) -> PlayerConfig:
    with path.open("rb") as stream:
        return PlayerConfig.model_validate(_json(stream.read(MAX_JSON + 1)))


class State(Model):
    configuration: PlayerConfiguration
    plan: Plan | None
    commits: tuple[Commit, ...] = Field(max_length=1024)
    revocations: tuple[Revocation, ...] = Field(max_length=1024)
    # Central's identify cue: read (the State's wire) and never acted on; the Player offers no
    # `identify_output` capability (hello_protocol).
    identify_output: IdentifyOutput | None = None
    identify_expires_at: Instant | None = None
    delivery_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    delivery_sequence: int = Field(ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def coherent_identify(self):
        if self.identify_expires_at is not None and self.identify_output is None:
            raise ValueError("identify_expiry_without_request")
        if self.identify_output is not None and self.identify_expires_at is None:
            raise ValueError("identify_request_without_expiry")
        return self


class BootContext(Model):
    """The equipment and run this Player enrolls as: the Pi's serial-derived device_id and a
    fresh `run_id` for this Player process. The run id is not the kernel boot id
    (`PlayerService.boot_id`, which the node link names); it travels as the enrollment's
    `boot_id` field, so a restarted Player is a new enrollment."""

    device_id: str = Field(pattern=r"^device-[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


class Registration(Model):
    player_id: str = Field(pattern=r"^p-[a-f0-9]{32}$")
    token: str = Field(min_length=32, max_length=256, repr=False)
    authority_epoch: int = Field(ge=1)


class Challenge(Model):
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: Instant


PI_SERIAL_PATH = Path("/sys/firmware/devicetree/base/serial-number")
CPUINFO_PATH = Path("/proc/cpuinfo")


def read_pi_serial() -> bytes | None:
    """Raw Pi hardware serial, preferring the devicetree file (present on all
    current Pi kernels) and falling back to the `Serial` line `/proc/cpuinfo`
    exposes when it is not. Returns None when neither source exists (dev
    machines, CI, non-Pi hardware) so callers can raise a clear error instead
    of fabricating an identity.
    """
    try:
        with PI_SERIAL_PATH.open("rb") as stream:
            raw = stream.read(READ_CAP)
        if raw:
            return raw
    except OSError:
        pass
    try:
        with CPUINFO_PATH.open("rb") as stream:
            for line in stream:
                label, sep, value = line.partition(b":")
                if sep and label.strip().lower() == b"serial":
                    return value
    except OSError:
        pass
    return None


def hardware_boot_context(serial_reader: Callable[[], bytes | None] = read_pi_serial) -> BootContext:
    """The Player's boot context: `device_id` from the Pi hardware serial by the one
    kind+hash scheme (contracts.equipment.equipment_device_id, kind="pi"), so one Pi keeps one
    device_id (0008: device_id is the immutable serial), and a fresh `run_id` for this run.
    """
    try:
        raw = serial_reader()
    except OSError as error:
        raise ServiceError("boot_equipment_identity") from error
    device_id = equipment_device_id("pi", raw) if raw is not None else None
    if device_id is None:
        raise ServiceError("boot_equipment_identity")
    return BootContext(device_id=device_id, run_id=str(uuid.uuid4()))


@dataclass(frozen=True)
class Download:
    epoch: int
    plan_id: str
    revision: int
    layer: Layer


class _ChunkBridge:
    """A download's bytes, from the network task to the cache worker. The network side `end`s it
    with why it stopped: a download error is the content's failure, and the cache worker raises
    ServiceError("download"); the Player's own stop (its session ended, or the job's authority is
    gone) is AcquisitionStopped on both sides."""

    def __init__(self, authorized: Callable[[], bool]):
        self.queue = queue.Queue(maxsize=4)
        self.stopped = threading.Event()
        self.failure: Exception | None = None
        self.authorized = authorized

    def end(self, failure: Exception | None) -> None:
        """The network side is done: `failure` is the download error that ended it, None when it
        finished or the Player stopped it. Set before `stopped`, so the worker reads it."""
        self.failure = failure
        self.stopped.set()

    async def put(self, chunk: bytes | None):
        while not self.stopped.is_set() and self.authorized():
            try:
                self.queue.put_nowait(chunk)
                return
            except queue.Full:
                await asyncio.sleep(.005)
        raise AcquisitionStopped("download_stopped")

    def chunks(self):
        while not self.stopped.is_set() and self.authorized():
            try:
                chunk = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            if chunk is None:
                return
            if not self.authorized() or self.stopped.is_set():
                break
            yield chunk
        if self.failure is not None and self.authorized():
            raise ServiceError("download") from self.failure
        raise AcquisitionStopped("download_stopped")


class PlayerService:
    def __init__(self, config: PlayerConfig, identity: Identity,
                 outputs: tuple[OutputReport, ...], renderer: Renderer, dispatcher: Callable,
                 *, find_central: Callable[[], Awaitable[Found]], trust: Trust,
                 clock: Clock | None = None, client: httpx.AsyncClient | None = None,
                 time_client: httpx.AsyncClient | None = None,
                 clock_record: Callable[[], ClockRecord | None] = RunClockRecord().read,
                 websocket_connect=None, cache_factory=Cache, executor_factory=Executor,
                 node_link_client: NodeAppLinkClient | None = None,
                 boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
                 boot_context: BootContext, luts: LutWants, room_lights_dispatch: Callable | None = None,
                 node_bus_url: str = NODE_BUS_URL, release_digest: str = "unknown"):
        self.config, self.identity, self.outputs = config, identity, outputs
        # The LUTs the renderer asks for (print look): fetched here, held there. The same
        # PlayerLuts the renderer was built with (`main`).
        self.luts = luts
        self.renderer, self.dispatcher = renderer, dispatcher
        # The room-lights document's hop onto the GLib thread: its own lane (`main`), so a light
        # switching never takes a control dispatch's slot; the newest pending document wins
        # (`room_lights_changed`).
        self._room_lights_dispatch = room_lights_dispatch or dispatcher
        self._room_lights_pending: RoomLightsDocument | None = None
        # What the renderer last drew with (the document and its digest), and the reader's health:
        # reported on the Player's line.
        self._room_lights_document: RoomLightsDocument | None = None
        self._room_lights_drawn: str | None = None
        self._room_lights_following: Callable[[], bool] | None = None
        # The Node's bus, where the room lights are followed and the Player's own line is written
        # from `start` (`node_bus_url`; player/player_line.py). `release_digest`: its birth's.
        self._node_bus_url = node_bus_url
        self._room_lights_reader: WallReader | None = None
        self.line = PlayerLine(release_digest, url=node_bus_url)
        self._line_started = False
        # What the line was last given, and when the drawn report is due again (`_report_on_line`).
        self._frames_reported: tuple | None = None
        self._drawn_reported: RoomLightsDrawn | None = None
        self._drawn_due = -math.inf
        self.clock = clock or SystemClock()
        self.mapping = TimeMapping(self.clock)
        self.link = CentralLink(trust, unauthorized=Unauthorized, client=client,
                                time_client=time_client, websocket_connect=websocket_connect)
        self.clock_record = clock_record
        self.cache_factory, self.executor_factory = cache_factory, executor_factory
        try:
            with boot_id_path.open("r") as stream:
                boot_id = stream.read(37).strip()
            self.boot_id = boot_id if re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot_id) else None
        except OSError:
            self.boot_id = None
        self.boot_context = boot_context
        self._find_central = find_central
        # The located Central and the registration it issued (U8): the only source of the
        # bearer. Kept across a failed cycle; `_located` says whether it is this cycle's.
        self._session: Session | None = None
        self._located = False
        self.cache = None
        self.executor = None
        self._owner = threading.get_ident()
        self._lock = threading.RLock()
        self._plan: Plan | None = None
        self._configuration: PlayerConfiguration | None = None
        self._offered = False
        self._cancelled: set[str] = set()
        self._seen_commits: deque[Commit] = deque(maxlen=2048)
        self._observations: deque[Observation] = deque(maxlen=128)
        self._outgoing: deque[Observation] = deque(maxlen=128)
        self._jobs: tuple[Download, ...] = ()
        self._verify_due = threading.Event()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="player-media")
        self._stop = threading.Event()
        self.node_link_client = node_link_client or NodeAppLinkClient()
        self._proof_active = False
        self._proof_generation = 0
        self._relink = threading.Event()
        # The running app-link loop's wake-up, set from any thread through its loop.
        self._link_wake: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None
        self._thread = None
        self._loop = None
        self._task = None
        self.last_fault: str | None = None
        self.last_fault_detail: str | None = None
        self._hello_epoch: int | None = None
        self._control_selection: ControlSelection | None = None
        self._highest_control_sequence = 0
        self._highest_control_delivery_id: str | None = None
        self._applied_proof_receipt: tuple[Session, ControlAppliedReceipt] | None = None
        self._control_proof_revision = 0
        # Dispatches abandoned at DISPATCH_DEADLINE: while one is unreached the loop is late,
        # every dispatch is MainLoopLate at once.
        self._late_posts = LatePosts()

    def fault(self, code: str, *, detail: str | None = None):
        if self.last_fault != code:
            LOG.warning("player fault: %s", code if detail is None else f"{code} {detail}")
        self.last_fault, self.last_fault_detail = code, detail

    def _main_loop_late(self) -> None:
        """The GLib loop missed a dispatch deadline: raise the fault, never touch readiness. Central keeps the last readiness it accepted, so committed groups stand; it
        commits no new group once that report is older than readiness_seconds, and a long
        stall reads as player-silent, as before G8 (E-G8-8 decision: a late loop must not
        withdraw prepared playback)."""
        self.fault("main_loop_late")

    def room_lights_changed(self, document: RoomLightsDocument) -> None:
        """A newer room-lights document (player/room_lights.py, on the WallReader's thread): handed to
        the renderer on the GLib thread, which draws every Frame again with it. It never touches the
        executor, the plan, the configuration or the control State's delivery; it is heard of only
        through the Player's line (`room_lights_drawn`, each Frame's light). Several arriving before the loop runs the hop draw once,
        newest."""
        with self._lock:
            idle = self._room_lights_pending is None
            self._room_lights_pending = document
        if idle:
            self._room_lights_dispatch(self._draw_room_lights)

    def _draw_room_lights(self) -> None:
        with self._lock:
            document, self._room_lights_pending = self._room_lights_pending, None
        if document is None:
            return
        self.renderer.set_room_lights(document)
        with self._lock:
            self._room_lights_document = document
            self._room_lights_drawn = room_lights_digest(document)

    def watch_room_lights_reader(self, following: Callable[[], bool]) -> None:
        """The room-lights reader's health, asked at every report (thread-safe, never blocks)."""
        with self._lock:
            self._room_lights_following = following

    def room_lights_drawn(self) -> RoomLightsDrawn:
        """What the Frames were last drawn with (None before the first document) and whether the
        reader follows the Node's WALL mirror now (False before one is watched)."""
        with self._lock:
            drawn, following = self._room_lights_drawn, self._room_lights_following
        return RoomLightsDrawn(drawn=drawn, following=following is not None and bool(following()))

    def frame_reports(self) -> FrameReports:
        """Every Frame the Player draws now (its control State's bindings): the binding generation,
        its Output, whether its picture is on screen, and the light on it at its centre for the
        room-lights document last drawn (None before the first; `light_lux_on_frame`, capped at
        LUX_MAX). On the GLib thread."""
        self._main()
        with self._lock:
            configuration, document, executor = self._configuration, self._room_lights_document, self.executor
        reports = []
        for binding in configuration.bindings if configuration is not None else ():
            lux = light_lux_on_frame(binding.lighting, document)
            try:
                reports.append(FrameReport(
                    frame_id=binding.frame_id, generation=binding.generation, output_id=binding.output_id,
                    showing=executor is not None and executor.showing(binding),
                    light_lux=None if lux is None else min(lux, LUX_MAX)))
            except ValidationError:   # an id the report's contract refuses: left out, never a fault
                LOG.debug("player: no Frame report for %s", binding.frame_id)
        return FrameReports(frames=tuple(reports))

    def _report_on_line(self) -> None:
        """On the GLib thread, every main-loop turn: the Player's line gets `frames` whenever a Frame's
        report changes, and `room-lights-drawn` whenever it changes and at least every
        ROOM_LIGHTS_DRAWN_REPEAT_SECONDS, unchanged too (Central judges it by age). The line holds
        each and publishes it when attached; nothing here waits on the bus."""
        frames = self.frame_reports()
        if frames.frames != self._frames_reported:
            self.line.put_frames(frames)
            self._frames_reported = frames.frames
        drawn, now = self.room_lights_drawn(), self.clock.monotonic()
        if drawn != self._drawn_reported or now >= self._drawn_due:
            self.line.put_drawn(drawn)
            self._drawn_reported, self._drawn_due = drawn, now + ROOM_LIGHTS_DRAWN_REPEAT_SECONDS

    def _publish_player(self) -> None:
        """Tell the renderer the Player's registered id, which a calibration target's QR names
        (decision 0026), or None while it has none: on every state applied and every retry, so
        a registration dropped on 401 or a failed cycle is cleared and a new one is drawn."""
        self._main()
        registration = self.registration
        self.renderer.set_player(registration.player_id if registration is not None else None)

    def _uplink_fault(self, code: str, error: UplinkError) -> None:
        """`code` with the console line (plus the clock summary for TIME/TLS) as its detail."""
        self.fault(code, detail=failure_text(error, clock=self.clock_record()))

    def _fault_for(self, error: Exception) -> None:
        """Names one session-ending failure of run()'s cycle (R9): a ServiceError is its own
        code; an UplinkError is "<cause>_<reason>" with its console detail; anything else is
        the bounded "player_error". Only the transport (CentralLink, locate) names network
        errors, so a local failure (ENOSPC, a bug) is never reported as a network cause."""
        if isinstance(error, ServiceError):
            self.fault(str(error))
        elif isinstance(error, UplinkError):
            self._uplink_fault(f"{error.cause}_{error.reason}", error)
        else:
            self.fault("player_error", detail=type(error).__name__)

    def _main(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Player control requires the renderer thread")

    async def dispatch(self, callback):
        """Run `callback` on the GLib thread. MainLoopLate when it has not started within
        DISPATCH_DEADLINE (it is abandoned then and never runs), and at once while an abandoned
        one is unreached: a late loop is never a capacity refusal and never fills the
        dispatcher's slots (E-G8-7). A callback that started before the deadline cannot be
        abandoned, so its outcome is awaited and returned, never MainLoopLate: its effects
        landed (E-G8-10). One that never returns blocks the GLib thread: its probes go
        unanswered and the broker's probe kill ends that Player."""
        source, abandon = self._late_posts.post(self.dispatcher, callback)
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()

        def settle(done) -> None:
            def deliver() -> None:
                if waiter.done():
                    return
                if done.cancelled():
                    waiter.cancel()
                elif done.exception() is not None:
                    waiter.set_exception(done.exception())
                else:
                    waiter.set_result(done.result())
            with contextlib.suppress(RuntimeError):     # this loop already closed
                loop.call_soon_threadsafe(deliver)

        source.add_done_callback(settle)
        try:
            finished, _ = await asyncio.wait((waiter,), timeout=DISPATCH_DEADLINE)
            if not finished:
                if abandon():
                    raise MainLoopLate("main_loop_late")
                await asyncio.wait((waiter,))       # already running: it completes
        finally:
            if not waiter.done():
                abandon()
                waiter.cancel()
        try:
            return waiter.result()
        except LoopLate as error:
            raise MainLoopLate("main_loop_late") from error
        except DispatchRefused as error:
            raise ServiceError(str(error)) from error

    async def _publish_player_on_retry(self) -> None:
        """A failed cycle's `_publish_player` (best effort: never let its dispatch replace the
        cycle's error)."""
        try:
            await self.dispatch(self._publish_player)
        except Exception as error:
            LOG.debug("player: could not publish the Player on retry: %s", type(error).__name__)

    @property
    def session(self) -> Session:
        """This cycle's Session; ServiceError("central_origin_unavailable") until this cycle
        has located Central. Every Central URL and every bearer comes from it."""
        if self._session is None or not self._located:
            raise ServiceError("central_origin_unavailable")
        return self._session

    @property
    def central(self) -> LocatedCentral:
        """The located Central of this cycle (see `session`)."""
        return self.session.central

    @property
    def registration(self) -> Registration | None:
        """The registration the Session's origin issued, if any. Read-only: it changes only
        through Session.enrolled / unregistered / relocated."""
        return None if self._session is None else self._session.registration

    async def locate_central(self) -> LocatedCentral:
        """await find_central() (R1: cmdline root, else the saved root); keeps the
        result for the session. A located origin other than the one that issued the
        registration drops it (Session.relocated), so the next step re-enrolls there.
        Raises UplinkError."""
        found = await self._find_central()
        previous = self._session
        self._session = (Session(found.central) if previous is None
                         else previous.relocated(found.central))
        if self._session is not previous:
            with self._lock:
                self._applied_proof_receipt = None
                self._control_proof_revision += 1
        self._located = True
        LOG.info("player: central %s (%s root %s)", found.central.origin, found.source,
                 found.root)
        return found.central

    async def request(self, method: str, path: str, *, body=None, authenticated=True) -> dict:
        value, _ = await self._request_with_receipt(method, path, body=body, authenticated=authenticated)
        return value

    async def _request_with_receipt(self, method: str, path: str, *, body=None,
                                    authenticated=True, time=False) -> tuple[dict, tuple[float, float]]:
        async with self.link.stream(self.session, method, path, body=body,
                                    authenticated=authenticated, time=time) as response:
            await self._check_status(response, path)
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ServiceError("encoded_response")
            if response.headers.get("content-type", "").split(";")[0] != "application/json":
                raise ServiceError("response_type")
            self._length(response, MAX_JSON)
            data = bytearray()
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                if len(data) + len(chunk) > MAX_JSON:
                    raise ServiceError("body_limit")
                data.extend(chunk)
            # Local JSON/schema work is not transport latency. Keep its delay
            # visible to the later sample-age gate instead of inflating RTT.
            received = self.clock.utc(), self.clock.monotonic()
            return _json(bytes(data)), received

    async def _check_status(self, response, target: str):
        """409 is StaleFeedback; any other non-200 is refusal(...) (401 and 3xx never get
        here: CentralLink refuses them)."""
        if response.status_code == 409:
            raise StaleFeedback("state_changed")
        if response.status_code != 200:
            raise await read_refusal(response, self.central.origin.url(target))

    @staticmethod
    def _length(response, maximum):
        value = response.headers.get("content-length")
        if value is None:
            return None
        if not value.isascii() or not value.isdigit() or len(value) > 20:
            raise ServiceError("response_length")
        length = int(value)
        if length > maximum:
            raise ServiceError("body_limit")
        return length

    async def enroll(self):
        session = self.session
        challenge = Challenge.model_validate(await self.request("POST", "/v1/enrollment/challenge",
            body={"public_key": self.identity.public_key}, authenticated=False))
        enrollment = self.identity.enrollment(
            challenge.nonce,
            self.outputs,
            device_id=self.boot_context.device_id,
            boot_id=self.boot_context.run_id,
        )
        registered = Registration.model_validate(await self.request("POST", "/v1/enrollment/register",
            body=enrollment.model_dump(mode="json"), authenticated=False))
        # Issued by session's origin, so it joins that Session and no other (U8).
        self._session = session.enrolled(registered)
        self._hello_epoch = None
        self._control_selection = None
        self._highest_control_sequence = 0
        self._highest_control_delivery_id = None
        with self._lock:
            self._applied_proof_receipt = None
            self._control_proof_revision += 1
            self._offered = False
            self._jobs = ()
        self._outgoing.clear()
        if self.executor is None:
            try:
                self.cache = await asyncio.get_running_loop().run_in_executor(self._worker,
                    lambda: self.cache_factory(
                        Path(self.config.cache_dir) if self.config.cache_dir else None,
                        self.config.cache_bytes,
                    ))

                def create():
                    self._main()
                    self.executor = self.executor_factory(registered.player_id, self.cache,
                        self.renderer, self.clock, self.mapping)

                # A late loop never discards the registration: an abandoned create() never
                # ran, so it is posted again until the loop runs it (main waited the same way).
                while True:
                    try:
                        await self.dispatch(create)
                        break
                    except MainLoopLate:
                        self._main_loop_late()
                        if self._stop.is_set():
                            raise
                        await asyncio.sleep(REPORT_INTERVAL)
            except Exception as error:
                if self.cache is not None:
                    await asyncio.get_running_loop().run_in_executor(self._worker, self.cache.close)
                self.cache = self.executor = None
                self._session = session.unregistered()
                raise ServiceError("player_initialization") from error

    async def hello_protocol(self) -> None:
        """Negotiate before either state transport."""
        if self.registration is None:
            raise Unauthorized("not_registered")
        epoch = self.registration.authority_epoch
        if self._hello_epoch == epoch:
            return
        # No capability: the Player shows nothing on an Output without a Frame (Display Host's
        # curtain covers it), so it offers no identify (s3-page decision 1).
        offer = ControlHello(authority_epoch=epoch, schemas=(2,), capabilities=())
        response = await self.request("POST", "/v1/player/hello",
                                      body=offer.model_dump(mode="json"))
        selected = ControlSelection.model_validate(response)
        if selected.authority_epoch != epoch or selected.schema_version not in offer.schemas:
            raise ServiceError("control_selection_invalid")
        if any(capability not in offer.capabilities for capability in selected.capabilities):
            raise ServiceError("control_selection_invalid")
        self._control_selection = selected
        self._hello_epoch = epoch

    def _apply_state(self, state: State):
        self._main()
        if self.registration is None or (
            state.configuration.player_id != self.registration.player_id
            or state.configuration.authority_epoch != self.registration.authority_epoch
        ):
            raise ServiceError("state_authority")
        sequence = state.delivery_sequence
        with self._lock:
            if sequence < self._highest_control_sequence:
                return "superseded"
            elif sequence == self._highest_control_sequence:
                if state.delivery_id != self._highest_control_delivery_id:
                    raise ServiceError("control_sequence_conflict")
            else:
                self._highest_control_sequence = sequence
                self._highest_control_delivery_id = state.delivery_id
                self._applied_proof_receipt = None
                self._control_proof_revision += 1
        if self.executor is None:
            return "parsed_execution_unavailable"
        with self._lock:
            rejected = False
            configuration = state.configuration
            self.executor.accept_configuration(configuration)
            if self._configuration is None or configuration.authority_epoch != self._configuration.authority_epoch:
                self._plan = None
                self._seen_commits.clear()
                self._cancelled.clear()
                self._jobs = ()
                self._observations.clear()
            self._configuration = configuration
            self._offered = state.plan is not None
            if state.plan is not None:
                changed = self.executor.accept_plan(state.plan)
                if changed:
                    self._seen_commits.clear()
                    self._cancelled.clear()
                self._plan = state.plan
            for revoked in state.revocations:
                self.executor.accept_revocation(revoked)
                if revoked.mode == "cancel":
                    self._cancelled.update(revoked.assignment_ids)
            # Revocation/configuration and this draw are one ordered GLib callback.
            self.executor.prepare_imminent()
            for commit in state.commits:
                if commit in self._seen_commits:
                    continue
                try:
                    self.executor.accept_commit(commit)
                except AuthorityError:
                    # Replayed grants can refer to readiness invalidated locally.
                    # A fresh report is required; never infer replacement authority.
                    self.fault("stale_commit")
                    rejected = True
                self._seen_commits.append(commit)
            # Every bound Frame's paper profile LUT is fetched before its first draw.
            self.luts.want(tuple(binding.lighting.paper.profile for binding in configuration.bindings
                                 if binding.lighting is not None and binding.lighting.paper.profile is not None))
            self._publish_player()
            self.tick_main()
            return "rejected" if rejected else "applied"

    async def _ack_control(self, state: State, result: str) -> None:
        session = self._session
        selection = self._control_selection
        if (result == "superseded"
                or session is None or session.registration is None or selection is None
                or selection.authority_epoch != session.registration.authority_epoch):
            return
        registration = session.registration
        with self._lock:
            if result != "applied":
                self._applied_proof_receipt = None
                self._control_proof_revision += 1
            revision = self._control_proof_revision
        report = ControlAck(authority_epoch=registration.authority_epoch,
                            delivery_id=state.delivery_id, result=result)
        # A superseded delivery can return accepted=false; the next state retries with a
        # fresh challenge. This is not plan readiness or OS management evidence.
        response = await self.request("POST", "/v1/player/control-acks",
                                      body=report.model_dump(mode="json"))
        if result != "applied":
            return
        try:
            outcome = ControlAckResponse.model_validate(response, strict=True)
        except ValidationError:
            LOG.debug("player: applied ACK receipt invalid")
            return
        receipt = outcome.receipt
        if (not outcome.accepted or receipt is None
                or receipt.authority_epoch != registration.authority_epoch
                or receipt.delivery_id != state.delivery_id
                or receipt.delivery_sequence != state.delivery_sequence):
            return
        with self._lock:
            if (self._control_proof_revision == revision and self._session is session
                    and session.registration is registration
                    and self._highest_control_sequence == receipt.delivery_sequence
                    and self._highest_control_delivery_id == receipt.delivery_id
                    and self._applied_proof_receipt != (session, receipt)):
                self._applied_proof_receipt = (session, receipt)
                self._wake_link_loop()

    def tick_main(self):
        self._main()
        if self.executor is not None:
            self.executor.prepare_imminent()
            observations = self.executor.tick()
            with self._lock:
                for observation in observations:
                    if len(self._observations) == self._observations.maxlen:
                        self.fault("observation_capacity")
                    self._observations.append(observation)
        self._report_on_line()

    def _feedback(self):
        self._main()
        with self._lock:
            readiness = None
            if self.executor is not None and self._plan is not None:
                readiness = self.executor.readiness()
                if any(failure.code in ("decode", "integrity") for failure in readiness.failures):
                    self._verify_due.set()
                self._jobs = tuple(Download(self._plan.authority_epoch, self._plan.plan_id,
                    self._plan.revision, layer) for layer in self._plan.layers
                    if layer.variant is not None and layer.assignment_id not in readiness.secured
                    and layer.assignment_id not in self._cancelled) if self._offered else ()
            observations = tuple({observation.assignment_id: observation for observation in
                self._observations if self._observation_current(observation)}.values())
            self._observations.clear()
            return readiness, observations

    def _observation_current(self, observation):
        with self._lock:
            return (self._plan is not None and self.registration is not None
                and observation.authority_epoch == self.registration.authority_epoch
                and (observation.authority_epoch, observation.plan_id, observation.revision) ==
                    (self._plan.authority_epoch, self._plan.plan_id, self._plan.revision)
                and any(layer.assignment_id == observation.assignment_id for layer in self._plan.layers))

    def _authorized(self, job: Download):
        with self._lock:
            plan, configuration = self._plan, self._configuration
            return (not self._stop.is_set() and self._offered and plan is not None
                and configuration is not None and job.epoch == configuration.authority_epoch
                and self.registration is not None and job.epoch == self.registration.authority_epoch
                and (job.epoch, job.plan_id, job.revision) == (plan.authority_epoch, plan.plan_id, plan.revision)
                and job.layer in plan.layers and job.layer.assignment_id not in self._cancelled
                and configuration.authorizes(job.layer)
                and self.clock.utc() < min(job.layer.end, plan.valid_until))

    async def download(self, job: Download) -> bool:
        if not self._authorized(job):
            return False
        if self.executor is None:
            return False
        # Validate reusable local bytes before opening an HTTP stream. The cache has
        # no durable index, so this check always hashes the exact candidate.
        cached = await asyncio.get_running_loop().run_in_executor(
            self._worker, self.executor.resolve_cached, job.layer.assignment_id
        )
        if cached:
            return True
        variant = job.layer.variant
        target = variant.path  # by name: a re-make keeps it
        bridge = _ChunkBridge(lambda: self._authorized(job))
        worker = None
        failure = None
        try:
            async with self.link.stream(self.session, "GET", target, timeout=120) as response:
                await self._check_status(response, target)
                if (response.headers.get("content-type", "").split(";")[0] != variant.media_type
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise ServiceError("media_type")
                # The bytes are checked against what Central served, not the plan's digest:
                # a re-made copy is other bytes of the same content.
                length = self._length(response, MAX_VARIANT_BYTES)
                if not length:
                    raise ServiceError("media_length")
                sha256 = served_sha256(response.headers.get("repr-digest"))
                worker = asyncio.get_running_loop().run_in_executor(self._worker,
                    functools.partial(self.executor.acquire, job.layer.assignment_id,
                                      bridge.chunks(), sha256=sha256, size=length))
                size = 0
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    size += len(chunk)
                    if worker.done():
                        # Cache may have verified an existing whole file without
                        # consuming bytes; it owns this independent success gate.
                        return worker.result()
                    if size > length:
                        raise ServiceError("media_length")
                    await bridge.put(chunk)
                if size != length:
                    raise ServiceError("media_length")
                await bridge.put(None)
                return await asyncio.shield(worker)
        except AcquisitionStopped:
            raise               # the job's authority is gone: the Player's own stop
        except Exception as error:
            failure = error     # a download error (reset, short body, overrun): the content's
            raise
        finally:
            # No HTTP failure/cancellation leaves the worker blocked on queue.get. Cancellation
            # (the session ended) is a BaseException, so it ends the bridge as the Player's stop.
            bridge.end(failure)
            if worker is not None:
                await asyncio.shield(worker)

    async def fetch_lut(self, ref: LutRef) -> bool:
        """Hold the LUT `ref` names for the renderer (print look, rule 3), from Central only (R11).
        A whole verified file the cache holds under `ref.key` is used with no request; otherwise
        GET `ref.path`, refused unless it is LUT_MEDIA_TYPE, identity-encoded, exactly LUT_BYTES
        long (`Content-Length`) and its bytes match the served `Repr-Digest`, as for a Variant;
        then kept through the media cache (pinned "luts") and held. False when there is no cache
        yet (before enrollment)."""
        cache = self.cache
        if cache is None:
            return False
        loop = asyncio.get_running_loop()
        held = await loop.run_in_executor(self._worker, cache.path_named, ref.key)
        if held is not None:
            body = await loop.run_in_executor(self._worker, held.read_bytes)
            if len(body) == LUT_BYTES:
                self.luts.hold(ref, body)
                return True
        async with self.link.stream(self.session, "GET", ref.path, timeout=30) as response:
            await self._check_status(response, ref.path)
            if (response.headers.get("content-type", "").split(";")[0] != LUT_MEDIA_TYPE
                    or response.headers.get("content-encoding", "identity") != "identity"):
                raise ServiceError("lut_type")
            if self._length(response, LUT_BYTES) != LUT_BYTES:
                raise ServiceError("lut_length")
            sha256 = served_sha256(response.headers.get("repr-digest"))
            body = bytearray()
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                if len(body) + len(chunk) > LUT_BYTES:
                    raise ServiceError("lut_length")
                body.extend(chunk)
        if len(body) != LUT_BYTES:
            raise ServiceError("lut_length")
        try:
            await loop.run_in_executor(self._worker, functools.partial(
                cache.secure_named, ref.key, (bytes(body),), "luts", sha256=sha256, size=LUT_BYTES))
        except CacheError as error:
            raise ServiceError("lut_digest") from error
        self.luts.hold(ref, bytes(body))
        return True

    async def _attempt(self, attempts: dict, key, fetch: Callable[[], Awaitable[bool]],
                       fault: str) -> None:
        """One fetch of `key` (a media download or a LUT) on its own retry schedule: a failure is
        the `fault` and retried after MEDIA_RETRY_BACKOFF; a 401 or a redirect ends the cycle.
        A fetch the Player stopped itself (AcquisitionStopped) is no fault."""
        count, _ = attempts.get(key, (0, 0))
        try:
            success = await fetch()
        except Unauthorized:
            raise
        except AcquisitionStopped:
            success = False
        except UplinkError as error:
            if error.cause is Cause.REDIRECT:
                raise           # ends the cycle: run() locates again (0014)
            success = False
            self._uplink_fault(fault, error)
        except (httpx.HTTPError, ServiceError, TimeoutError, OSError, AuthorityError):
            success = False
            self.fault(fault)
        attempts[key] = (min(count + 1, 3), self.clock.monotonic()
            + (1 if success else MEDIA_RETRY_BACKOFF[min(count, 3)]))

    async def _media_loop(self):
        attempts: dict[Download, tuple[int, float]] = {}
        lut_attempts: dict[LutRef, tuple[int, float]] = {}
        maintained_at, verified_at = -math.inf, -math.inf
        while not self._stop.is_set():
            with self._lock:
                jobs = self._jobs
            attempts = {job: value for job, value in attempts.items() if job in jobs}
            for job in jobs:
                if self.clock.monotonic() < attempts.get(job, (0, 0))[1] or not self._authorized(job):
                    continue
                await self._attempt(attempts, job, functools.partial(self.download, job), "media_download")
            wanted = self.luts.wanted()
            lut_attempts = {ref: value for ref, value in lut_attempts.items() if ref in wanted}
            for ref in wanted:
                if self.clock.monotonic() >= lut_attempts.get(ref, (0, 0))[1]:
                    await self._attempt(lut_attempts, ref, functools.partial(self.fetch_lut, ref), "lut_download")
            if self.executor is not None and self.clock.monotonic() - maintained_at >= 2:
                await asyncio.get_running_loop().run_in_executor(self._worker,
                                                               self.executor.maintain_cache)
                maintained_at = self.clock.monotonic()
            if self.executor is not None and (self._verify_due.is_set()
                                             or self.clock.monotonic() - verified_at >= 10):
                self._verify_due.clear()
                await asyncio.get_running_loop().run_in_executor(self._worker,
                                                               self.executor.verify_secured)
                verified_at = self.clock.monotonic()
            await asyncio.sleep(.1)

    @staticmethod
    def _state(body: dict) -> State:
        return State.model_validate(body)

    async def poll_state(self):
        body, _ = await self._request_with_receipt("GET", "/v1/player/state")
        state = self._state(body)
        result = await self.dispatch(lambda: self._apply_state(state))
        await self._ack_control(state, result)

    async def probe_time(self) -> bool:
        if self.registration is None:
            raise Unauthorized("not_registered")
        sent_utc, sent_monotonic = self.clock.utc(), self.clock.monotonic()
        body, (received_utc, received_monotonic) = await self._request_with_receipt(
            "GET", "/v1/player/time", time=True
        )
        sample = PlayerTime.model_validate(body)

        def apply() -> bool:
            self._main()
            if self.registration is None:
                return False
            return self.mapping.apply_probe(
                sample_epoch=sample.authority_epoch,
                authority_epoch=self.registration.authority_epoch,
                sample_player_id=sample.player_id,
                player_id=self.registration.player_id,
                server_time=sample.server_time,
                sent_utc=sent_utc,
                sent_monotonic=sent_monotonic,
                received_utc=received_utc,
                received_monotonic=received_monotonic,
                applied_utc=self.clock.utc(),
                applied_monotonic=self.clock.monotonic(),
            )

        return await self.dispatch(apply)

    async def _time_loop(self):
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self.probe_time()
            except Unauthorized:
                raise
            except UplinkError as error:
                if error.cause is Cause.REDIRECT:
                    raise               # ends the cycle: run() locates again (0014)
                if error.cause is Cause.LOCAL and error.reason == "pool":
                    raise               # ends the cycle: the next one builds fresh clients
                self._uplink_fault("clock_probe", error)
            except MainLoopLate:
                self._main_loop_late()
            except (httpx.HTTPError, ServiceError, TimeoutError, ValueError):
                self.fault("clock_probe")
            await asyncio.sleep(max(0, 1 - (asyncio.get_running_loop().time() - started)))

    async def _poll_after_stale(self) -> None:
        """Central refused feedback as stale: fetch state; a late loop applies it next turn."""
        try:
            await self.poll_state()
        except MainLoopLate:
            self._main_loop_late()

    async def _control_loop(self):
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self.poll_state()
                readiness, observations = await self.dispatch(self._feedback)
            except MainLoopLate:
                # Nothing is posted: the readiness Central holds stays (E-G8-8 decision).
                self._main_loop_late()
                await asyncio.sleep(max(0, REPORT_INTERVAL - (asyncio.get_running_loop().time() - started)))
                continue
            if readiness is not None:
                try:
                    await self.request("POST", "/v1/player/readiness", body=readiness.model_dump(mode="json"))
                except StaleFeedback:
                    await self._poll_after_stale()
            for observation in observations:
                if len(self._outgoing) == self._outgoing.maxlen:
                    self.fault("observation_capacity")
                self._outgoing.append(observation)
            await asyncio.sleep(max(0, REPORT_INTERVAL - (asyncio.get_running_loop().time() - started)))

    async def _observation_loop(self):
        # A slow observation endpoint cannot consume the readiness reporting slot.
        while not self._stop.is_set():
            if not self._outgoing:
                await asyncio.sleep(.05)
                continue
            observation = self._outgoing.popleft()
            if self._observation_current(observation):
                try:
                    await self.request("POST", "/v1/player/observations",
                                       body=observation.model_dump(mode="json"))
                except StaleFeedback:
                    await self._poll_after_stale()

    async def _websocket_loop(self):
        async with self.link.websocket(self.session, "/v1/player/session",
                                       max_size=MAX_JSON) as socket:
            async for message in socket:
                body = _json(message)
                if body.pop("type", None) != "state":
                    raise ServiceError("message_type")
                state = self._state(body)
                try:
                    result = await self.dispatch(lambda: self._apply_state(state))
                except MainLoopLate:
                    # Never started, so never applied; the next delivery or poll carries it.
                    self._main_loop_late()
                    continue
                await self._ack_control(state, result)
            raise ServiceError("session_closed")

    async def _app_link_loop(self) -> None:
        """Link this process to its latest applied control receipt over the node's app-link
        socket, apart from Central liveness and rendering.

        Only the process-local enrollment key enters the blocking socket worker. A new
        delivery, a re-enrollment or a relocation invalidates that worker before it signs or
        sends a response.
        """
        recorded: tuple[Session, ControlAppliedReceipt] | None = None
        wake = asyncio.Event()
        self._link_wake = (asyncio.get_running_loop(), wake)
        while self._proof_active and not self._stop.is_set():
            # Cleared before reading the receipt, so one that lands during a turn wakes the next.
            wake.clear()
            backoff = False
            if self._relink.is_set():
                # The broker could not deliver the recorded link: prove it again.
                self._relink.clear()
                recorded = None
            session = self._session
            registration = session.registration if session is not None else None
            generation = self._proof_generation
            with self._lock:
                applied = self._applied_proof_receipt
                revision = self._control_proof_revision
            if (registration is not None and applied is not None
                    and applied[0] is session and applied != recorded
                    and self.boot_id):
                receipt = applied[1]

                def applied_current() -> bool:
                    with self._lock:
                        return (self._proof_active and self._proof_generation == generation
                                and not self._stop.is_set() and self._session is session
                                and session.registration is registration
                                and self._control_proof_revision == revision
                                and self._applied_proof_receipt == applied
                                and self._highest_control_sequence == receipt.delivery_sequence
                                and self._highest_control_delivery_id == receipt.delivery_id)

                try:
                    outcome = await asyncio.to_thread(
                        self.node_link_client.exchange_applied, identity=self.identity,
                        player_id=registration.player_id,
                        authority_epoch=registration.authority_epoch,
                        device_id=self.boot_context.device_id,
                        kernel_boot_id=self.boot_id, receipt=receipt,
                        enrollment_current=applied_current,
                    )
                    if outcome == "recorded" and applied_current():
                        recorded = applied
                    elif outcome == "rejected":
                        backoff = True
                        LOG.warning("player: the broker refused the app link; retrying in %.0f s",
                                    APP_LINK_RETRY)
                    else:
                        LOG.info("player: app link stale (a newer control delivery or "
                                 "enrollment); retrying on the next applied delivery")
                except (ValueError, OSError, TimeoutError) as error:
                    backoff = True
                    LOG.debug("player: node process linkage unavailable: %s", type(error).__name__)
                except Exception as error:
                    backoff = True
                    LOG.warning("player: node process linkage failed: %s", type(error).__name__)
            if backoff:
                await asyncio.sleep(APP_LINK_RETRY)
            else:
                # The next applied receipt or relink wakes this at once; the timeout is a net.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), APP_LINK_RETRY)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._proof_generation += 1
        self._proof_active = True
        proof_task = None
        attempt = 0
        try:
            while not self._stop.is_set():
                tasks = []
                session_started = None
                try:
                    self.link.open()        # fresh HTTP clients each cycle (CentralLink)
                    if not self._located:
                        await self.locate_central()
                    if self.registration is None:
                        await self.enroll()
                    if proof_task is None:
                        proof_task = asyncio.create_task(self._app_link_loop())
                    await self.hello_protocol()
                    # Reconnection reconciles authority before any download work. A late GLib
                    # loop does not end the session here: its loops raise main_loop_late.
                    for step in (self.probe_time, self.poll_state):
                        try:
                            await step()
                        except MainLoopLate:
                            self._main_loop_late()
                    session_started = self._loop.time()
                    tasks = [asyncio.create_task(self._control_loop()),
                             asyncio.create_task(self._time_loop()),
                             asyncio.create_task(self._media_loop()),
                             asyncio.create_task(self._observation_loop())]
                    if self.link.websocket_connect is not False:
                        tasks.append(asyncio.create_task(self._websocket_loop()))
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    attempt = 0
                except Unauthorized:
                    # Every failed cycle locates again. Central refused the registration, so
                    # it is dropped here; otherwise Session.relocated keeps it only if the
                    # new locate lands on the origin that issued it (U8).
                    self._located = False
                    if self._session is not None:
                        self._session = self._session.unregistered()
                    with self._lock:
                        self._applied_proof_receipt = None
                        self._control_proof_revision += 1
                    self.fault("registration_required")
                    if not self._stop.is_set():
                        await self._publish_player_on_retry()
                except Exception as error:
                    self._located = False
                    self._fault_for(error)
                    if not self._stop.is_set():
                        await self._publish_player_on_retry()
                finally:
                    if session_started is not None and self._loop.time() - session_started >= 30:
                        attempt = 0
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    await self.link.aclose()
                    with self._lock:
                        self._offered = False
                        self._jobs = ()
                if not self._stop.is_set():
                    await asyncio.sleep(BACKOFF[min(attempt, 3)])
                    attempt = min(attempt + 1, 3)
        except asyncio.CancelledError:
            pass
        finally:
            self._proof_active = False
            self._proof_generation += 1
            with self._lock:
                self._applied_proof_receipt = None
                self._control_proof_revision += 1
            if proof_task is not None:
                proof_task.cancel()
                # stop() may cancel this parent while it awaits the child.
                # Shield the child so cleanup waits for it without surfacing
                # that second cancellation to callers of run().
                while not proof_task.done():
                    try:
                        await asyncio.shield(proof_task)
                    except asyncio.CancelledError:
                        pass
            await self.link.aclose()
            if self.cache is not None:
                await asyncio.get_running_loop().run_in_executor(self._worker, self.cache.close)
            self._worker.shutdown(wait=True, cancel_futures=True)
            self._session, self._located = None, False

    def start(self):
        """The network thread, and the Node's bus (`start_node_bus`)."""
        self._main()
        if self._thread is not None:
            raise RuntimeError("service already started")
        self._thread = threading.Thread(target=lambda: asyncio.run(self.run()),
                                        name="player-network", daemon=False)
        self._thread.start()
        self.start_node_bus()

    def start_node_bus(self):
        """The room lights followed from the Node's WALL mirror, and the Player's own line written,
        each on its own thread, never blocking the Player (the bus away keeps the last document: the
        mirror; the line holds its reports until it attaches). `start` calls it; on the GLib thread."""
        self._main()
        if self._line_started:
            raise RuntimeError("node bus already started")
        self._line_started = True
        self.line.start()
        self._room_lights_reader = start_room_lights(self, self._node_bus_url)

    def request_relink(self):
        """Thread-safe: the proof loop forgets its recorded app link and proves it again now."""
        self._relink.set()
        self._wake_link_loop()

    def _wake_link_loop(self) -> None:
        """Thread-safe: the app-link loop takes its next turn now, not at its safety net."""
        if (link_wake := self._link_wake) is not None:
            loop, wake = link_wake
            with contextlib.suppress(RuntimeError):  # the loop has closed: nothing to wake
                loop.call_soon_threadsafe(wake.set)

    def stop(self):
        self._stop.set()
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        if (reader := self._room_lights_reader) is not None:   # asked to stop, never waited on here
            self._room_lights_reader = None
            reader.stop(0)
        if self._line_started:
            self.line.stop(0)

    @property
    def stopped(self):
        return self._thread is not None and not self._thread.is_alive()


def central_finder(config: PlayerConfig, resolution: Configured | Unconfigured, *,
                   transport: Transport) -> Callable[[], Awaitable[Found]]:
    """R1/U3's wiring for the Player (design §2.3): the cmdline root wins outright, and then
    the saved root (config.central_origin) is never parsed or validated, only noted as
    ignored; without a cmdline root the saved root is validated (UplinkError when it is not a
    Central root) and used. Neither: find_central fails "not_discovered" and the cycle
    retries."""
    saved: Origin | None = None
    if isinstance(resolution, Configured):
        if config.central_origin is not None:
            LOG.info("player: the saved central_origin is ignored: the kernel command line "
                     "names %s", resolution.root)
    else:
        saved = config.saved_root()
    return functools.partial(find_central, resolution, transport=transport, saved=saved)


def build_finder(config: PlayerConfig, *, transport: Transport,
                 cmdline_path: Path | None = None) -> Callable[[], Awaitable[Found]]:
    """main()'s first construction step (before the renderer): read the kernel command line at `cmdline_path`
    (default KERNEL_COMMAND_LINE; a missing file is "no cmdline"), resolve it (R1) and wire
    central_finder. Raises UplinkError for an unreadable or invalid cmdline, or an invalid
    saved root when the cmdline names no Central."""
    path = KERNEL_COMMAND_LINE if cmdline_path is None else cmdline_path
    resolution = resolve_central(read_kernel_command_line(path))
    return central_finder(config, resolution, transport=transport)


def main():
    parser = argparse.ArgumentParser(description="Photo Wall Player")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config(args.config)
    try:
        trust = Trust.public(Path(config.ca_file)) if config.ca_file else Trust.public()
        find = build_finder(config, transport=HttpTransport(trust=trust))
    except UplinkError as error:
        # A bad cmdline, saved root or trust store: exit 1, and the unit restarts.
        LOG.error("player: %s", failure_text(error, clock=None))
        raise SystemExit(1) from None
    identity = load_identity()
    boot_context = hardware_boot_context()
    discovery = discover_outputs()
    from gi.repository import GLib

    luts = PlayerLuts()         # one for both: the renderer asks, the service fetches
    dispatcher = MainLoopDispatcher(GLib)
    root = start_renderer(GLib, dispatcher, luts, discovery.outputs)
    service = PlayerService(
        config,
        identity,
        root.outputs,           # enrolled with their modes as built
        root.renderer,
        dispatcher,
        boot_context=boot_context,
        find_central=find,
        trust=trust,
        luts=luts,
        room_lights_dispatch=dispatcher.lane(2),    # one hop queued and one running, at most
    )
    for fault in (discovery.fault, root.fault):
        if fault:
            service.fault(fault)
    loop = GLib.MainLoop()
    closing = False

    def finish(*_):
        nonlocal closing
        closing = True
        service.stop()

    def tick():
        if closing and service.stopped:
            loop.quit()
            return False
        if not closing:
            try:
                service.tick_main()
            except Exception:
                service.fault("execution_failed")
                finish()
        return True

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    Tick(GLib, PLANNING_TICK_MS, tick)
    service.start()
    # Probes are answered at control dispatch's priority (own one-slot lane), never here.
    ProbeResponder(dispatcher, on_relink=service.request_relink,
                   gl_renderer=lambda: root.renderer.gl_renderer).start()
    try:
        with contextlib.suppress(KeyboardInterrupt):
            loop.run()
    finally:
        root.close()


if __name__ == "__main__":
    main()
