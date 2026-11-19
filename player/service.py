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
from contracts.liveness import SESSION_BACKOFF
from contracts.lut import LUT_BYTES, LUT_MEDIA_TYPE, LutRef
from contracts.models import (
    MAX_VARIANT_BYTES,
    Commit,
    IdentifyOutput,
    Instant,
    Layer,
    Model,
    OutputBinding,
    Plan,
    PlayerConfiguration,
    PlayerTime,
    Revocation,
)
from contracts.node_app_link import FrameBlack
from contracts.node_impeded import ImpededFrames
from contracts.node_link import NODE_BUS_URL
from contracts.node_state import FrameReport, FrameReports
from contracts.player_control import (
    ControlAck,
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
from contracts.time import Monotonic, SystemClock
from nodeapi.node import WallReader
from player.cache import Cache, CacheError
from player.central_link import CentralLink, Session, read_refusal
from player.executor import AcquisitionStopped, AuthorityError, Executor, may_still_play
from player.geometry import light_lux_on_frame
from player.identity import Identity, load_identity
from player.impediment import impeded
from player.luts import LutWants, PlayerLuts
from player.mainloop import DispatchRefused, LatePosts, LoopLate, MainLoopDispatcher, Tick
from player.output_discovery import discover_outputs
from player.player_line import PlayerLine
from player.probe_responder import ProbeResponder
from player.renderer_root import start_renderer
from player.rendering import Renderer
from player.report_outbox import ReportOutbox
from player.room_lights import start_room_lights
from player.show_clock import ShowClock
from uplink.causes import Cause, UplinkError
from uplink.clock import RunClockRecord
from uplink.diagnosis import failure_text
from uplink.fetch import Refused
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
# Seconds between two turns of the control loop: the held plan's feedback read on the GLib loop,
# and its readiness posted when it changed. Local: never a request of its own (Central pushes the
# state over the session socket; decision 0034).
FEEDBACK_INTERVAL = 0.5
# A failed media download retries on its own schedule; it is not a liveness contract.
MEDIA_RETRY_BACKOFF = (1, 5, 15, 60)
# How long a thread-to-main handoff may wait for the GLib loop before the Player raises the
# main_loop_late fault instead of waiting on a starved loop in silence (G8). Readiness
# is left as last sent: a late loop never withdraws prepared playback (E-G8-8 decision).
DISPATCH_DEADLINE = 2 * FEEDBACK_INTERVAL
# The planning tick's period (player/mainloop.py `Tick`): the executor's draw grace must outlast a
# commit in flight plus one of them (player/executor.py `DRAW_GRACE`).
PLANNING_TICK_MS = 33
LOG = logging.getLogger("photo_wall.player")


def _drawn_lux(binding: OutputBinding, document: RoomLightsDocument | None) -> float | None:
    """The light on `binding`'s Frame at its centre as drawn with `document`, capped at LUX_MAX (None
    before the first document): the one value the Frame's report carries and its impediment is
    judged by."""
    lux = light_lux_on_frame(binding.lighting, document)
    return None if lux is None else min(lux, LUX_MAX)


class ServiceError(ValueError):
    """A bounded diagnostic code, with no response body or credential attached."""


class Unauthorized(ServiceError):
    pass


class StaleFeedback(ServiceError):
    pass


class OriginUnavailable(ServiceError):
    """This cycle has no located Central (`PlayerService.session`): its cycle is ending, so a loop
    meeting it ends too (`ends_cycle`); never a request Central answered."""


REQUEST_FAILURES = (UplinkError, httpx.HTTPError, ServiceError, ValidationError)
"""What one failed request to Central raises (decision 0032): a fault the loop that sent it names,
then tries again at its next turn; only `ends_cycle` ones end the session."""


def ends_cycle(error: BaseException) -> bool:
    """Whether `error` ends run()'s cycle (decision 0032): a refused registration (Unauthorized), a
    redirect (run() locates again, 0014), or a request that never left its pool (the next cycle
    builds fresh clients, CentralLink); and a cycle that already lost its Central
    (OriginUnavailable). Any other failed request leaves the session, its plan and its downloads
    running."""
    return isinstance(error, Unauthorized | OriginUnavailable) or (isinstance(error, UplinkError) and (
        error.cause is Cause.REDIRECT or (error.cause is Cause.LOCAL and error.reason == "pool")))


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
    # Central's clock when it answered: a show-clock sample on every state (decision 0032), read
    # after Central's delivery digest, so a new reading never makes a new delivery.
    server_time: Instant

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
                 clock: Monotonic | None = None, client: httpx.AsyncClient | None = None,
                 time_client: httpx.AsyncClient | None = None,
                 clock_record: Callable[[], ClockRecord | None] = RunClockRecord().read,
                 websocket_connect=None, cache_factory=Cache, executor_factory=Executor,
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
        self._impeded_reported: ImpededFrames | None = None
        self._drawn_reported: RoomLightsDrawn | None = None
        self._drawn_due = -math.inf
        self.clock = clock or SystemClock()
        self.show_clock = ShowClock(self.clock.monotonic)
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
        # Observations for Central, each a state change, until Central answers (never dropped).
        self._reports = ReportOutbox()
        self._jobs: tuple[Download, ...] = ()
        self._verify_due = threading.Event()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="player-media")
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None
        self.last_fault: str | None = None
        self.last_fault_detail: str | None = None
        self._hello_epoch: int | None = None
        self._control_selection: ControlSelection | None = None
        self._highest_control_sequence = 0
        self._highest_control_delivery_id: str | None = None
        # The last acknowledgment Central answered (session, delivery id, result): the same state
        # again (a state sent again unchanged) is not acknowledged again.
        self._acknowledged: tuple[Session, str, str] | None = None
        # The last readiness Central answered, as (authority epoch, number): the executor numbers
        # it once per change and starts again at 1 in each epoch, so an unchanged readiness is not
        # posted again and a new epoch's first is.
        self._readiness_answered: tuple[int, int] | None = None
        # Dispatches abandoned at DISPATCH_DEADLINE: while one is unreached the loop is late,
        # every dispatch is MainLoopLate at once.
        self._late_posts = LatePosts()

    def fault(self, code: str, *, detail: str | None = None):
        if self.last_fault != code:
            LOG.warning("player fault: %s", code if detail is None else f"{code} {detail}")
        self.last_fault, self.last_fault_detail = code, detail

    def _main_loop_late(self) -> None:
        """The GLib loop missed a dispatch deadline: raise the fault, never touch readiness. Central keeps the last readiness it accepted, so committed groups stand and
        later grants still answer it (decision 0032: freshness is the readiness sequence a
        Commit echoes, not a report's age), and a long stall reads as player-silent, as before
        G8 (E-G8-8 decision: a late loop must not withdraw prepared playback)."""
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
        its Output, whether its picture is on screen, the light on it at its centre for the
        room-lights document last drawn (None before the first; `light_lux_on_frame`, capped at
        LUX_MAX), and since when it plays Central's continuation (decision 0032,
        `Executor.continuing_since`). On the GLib thread."""
        self._main()
        with self._lock:
            executor = self.executor
        reports = []
        for binding, document in self._drawn_bindings():
            lux = _drawn_lux(binding, document)
            try:
                reports.append(FrameReport(
                    frame_id=binding.frame_id, generation=binding.generation, output_id=binding.output_id,
                    showing=executor is not None and executor.showing(binding), light_lux=lux,
                    continuing_since=executor.continuing_since(binding) if executor is not None else None))
            except ValidationError:   # an id the report's contract refuses: left out, never a fault
                LOG.debug("player: no Frame report for %s", binding.frame_id)
        return FrameReports(frames=tuple(reports))

    def impeded_frames(self) -> ImpededFrames:
        """Every Frame Central's doubt blacks now (player/impediment.py `impeded`, owner 2026-10-27:
        "when the issue induces blackness on the frame"): judged against the room-lights document last
        drawn and the light drawn at its centre (`frame_reports`' `light_lux`), naming the grant its
        Output's last presented commit was drawn under. A Frame with no presented commit, or with no
        document drawn yet, is not impeded. Changes no drawing. On the GLib thread."""
        self._main()
        frames = []
        for binding, document in self._drawn_bindings():
            lux, grant = _drawn_lux(binding, document), self.renderer.drawn_grant(binding.output_id)
            if lux is None or grant is None:
                continue
            try:
                found = impeded(binding.frame_id, binding.output_id, grant, binding.lighting, document, lux)
            except ValidationError:   # an id the report's contract refuses: left out, never a fault
                LOG.debug("player: no impeded report for %s", binding.frame_id)
                continue
            if found is not None:
                frames.append(found)
        return ImpededFrames(frames=tuple(frames))

    def _drawn_bindings(self) -> list[tuple[OutputBinding, RoomLightsDocument | None]]:
        """Each Frame the Player draws now (its control State's bindings), with the room-lights
        document last drawn."""
        with self._lock:
            configuration, document = self._configuration, self._room_lights_document
        return [(binding, document) for binding in (configuration.bindings if configuration is not None else ())]

    def _report_on_line(self) -> None:
        """On the GLib thread, every main-loop turn: the Player's line gets `frames` whenever a Frame's
        report changes, `impeded` whenever which Frames are impeded changes (put whole, empty too, so
        "not impeded" is said, never left unsaid), and `room-lights-drawn` whenever it changes and at least every
        ROOM_LIGHTS_DRAWN_REPEAT_SECONDS, unchanged too (Central judges it by age). The line holds
        each and publishes it when attached; nothing here waits on the bus."""
        frames = self.frame_reports()
        if frames.frames != self._frames_reported:
            self.line.put_frames(frames)
            self._frames_reported = frames.frames
        stopped = self.impeded_frames()
        if stopped != self._impeded_reported:
            self.line.put_impeded(stopped)
            self._impeded_reported = stopped
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

    def _request_failed(self, error: Exception) -> None:
        """One failed request of a session's loop (decision 0032): raised again when it ends the
        cycle (`ends_cycle`), else named (`_fault_for`) and the loop tries again at its next turn."""
        if ends_cycle(error):
            raise error
        self._fault_for(error)

    def _fault_for(self, error: Exception) -> None:
        """Names one failure of run()'s cycle or of a session's request (R9): a ServiceError is its own
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
        """This cycle's Session; OriginUnavailable("central_origin_unavailable") until this cycle
        has located Central. Every Central URL and every bearer comes from it."""
        if self._session is None or not self._located:
            raise OriginUnavailable("central_origin_unavailable")
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
        self._located = True
        LOG.info("player: central %s (%s root %s)", found.central.origin, found.source,
                 found.root)
        return found.central

    async def request(self, method: str, path: str, *, body=None, authenticated=True) -> dict:
        value, _ = await self._request_with_receipt(method, path, body=body, authenticated=authenticated)
        return value

    async def _request_with_receipt(self, method: str, path: str, *, body=None,
                                    authenticated=True, time=False) -> tuple[dict, float]:
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
            # Local JSON/schema work is not transport latency: the receipt is read before it,
            # so a sample's round trip is the wire's alone.
            received = self.clock.monotonic()
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
            self._acknowledged = None
            self._offered = False
            self._jobs = ()
        self._reports.clear()
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
                        self.renderer, self.clock, self.show_clock)

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
                        await asyncio.sleep(FEEDBACK_INTERVAL)
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
                self._reports.clear()
            self._configuration = configuration
            self._offered = state.plan is not None
            if state.plan is not None:
                changed = self.executor.accept_plan(state.plan)
                if changed:
                    self._seen_commits.clear()
                    self._cancelled.clear()
                self._plan = state.plan
                self._reports.renewed(state.plan.plan_id, state.plan.revision)
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
        acknowledgment = (session, state.delivery_id, result)
        if self._acknowledged == acknowledgment:
            return      # Central answered this one: the same state again changes nothing
        report = ControlAck(authority_epoch=registration.authority_epoch,
                            delivery_id=state.delivery_id, result=result)
        # A superseded delivery can return accepted=false; the next state retries with a
        # fresh challenge. This is not plan readiness or OS management evidence. The receipt Central
        # answers an applied acknowledgment with is no longer read here (decision 0034, S4c).
        await self.request("POST", "/v1/player/control-acks", body=report.model_dump(mode="json"))
        self._acknowledged = acknowledgment

    def tick_main(self):
        self._main()
        if self.executor is not None:
            self.executor.prepare_imminent()
            for report in self.executor.tick():
                self._reports.put(report)
        self._report_on_line()

    def _feedback(self):
        self._main()
        with self._lock:
            readiness = None
            if self.executor is not None and self._plan is not None:
                readiness = self.executor.readiness()
                if readiness is not None and any(failure.code in ("decode", "integrity")
                                                  for failure in readiness.failures):
                    self._verify_due.set()
                secured = self.executor.secured_ids()
                now = self.show_clock.now()
                # Decision 0032: a layer is fetched while it may still play, a continuation's
                # listed photo included (`may_still_play`, the `_authorized` test).
                self._jobs = tuple(Download(self._plan.authority_epoch, self._plan.plan_id,
                    self._plan.revision, layer) for layer in self._plan.layers
                    if layer.variant is not None and layer.assignment_id not in secured
                    and layer.assignment_id not in self._cancelled
                    and may_still_play(self._plan, layer, now)) if self._offered else ()
            return readiness

    def _observation_current(self, observation):
        """Whether an observation is still Central's to judge (decision 0032): it names the held
        plan's epoch and plan at its revision or an earlier one (a Frame playing its continuation
        through an outage observes under the revision it held; Central records it against its
        source photo). Central judges the rest."""
        with self._lock:
            return (self._plan is not None and self.registration is not None
                and observation.authority_epoch == self.registration.authority_epoch
                and (observation.authority_epoch, observation.plan_id) ==
                    (self._plan.authority_epoch, self._plan.plan_id)
                and observation.revision <= self._plan.revision)

    def _authorized(self, job: Download):
        with self._lock:
            plan, configuration = self._plan, self._configuration
            return (not self._stop.is_set() and self._offered and plan is not None
                and configuration is not None and job.epoch == configuration.authority_epoch
                and self.registration is not None and job.epoch == self.registration.authority_epoch
                and (job.epoch, job.plan_id, job.revision) == (plan.authority_epoch, plan.plan_id, plan.revision)
                and job.layer in plan.layers and job.layer.assignment_id not in self._cancelled
                and configuration.authorizes(job.layer)
                and may_still_play(plan, job.layer, self.show_clock.now()))

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
            if ends_cycle(error):
                raise           # ends the cycle: run() locates again (0014) or builds fresh clients
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

    async def receive_state(self, body: dict, sent_monotonic: float,
                            received_monotonic: float) -> None:
        """One state Central pushed over the session socket (`body` without its "type"). It is
        also a show-clock sample: Central read its clock after the socket's opening request was
        sent at `sent_monotonic`, so any state anchors the clock before its plan is applied, and a
        plan is never held on an unanchored clock however Central's time route fares (decision
        0032). A stale observation waiting for a newer revision goes again under it."""
        state = self._state(body)

        def apply():
            self._main()
            self._sample_show_clock(state.configuration.player_id,
                                    state.configuration.authority_epoch, state.server_time,
                                    sent_monotonic, received_monotonic)
            return self._apply_state(state)

        result = await self.dispatch(apply)
        self._renew_stale_reports()
        await self._ack_control(state, result)

    def _renew_stale_reports(self) -> None:
        with self._lock:
            plan = self._plan
        if plan is not None:
            self._reports.renewed(plan.plan_id, plan.revision)

    def _sample_show_clock(self, player_id: str, authority_epoch: int, server_time: float,
                           sent_monotonic: float, received_monotonic: float) -> bool:
        """GLib thread: one Central answer as a show-clock sample, only when it names this
        registration (another's leaves the ShowClock untouched). True when it anchored or kept."""
        registration = self.registration
        if (registration is None or player_id != registration.player_id
                or authority_epoch != registration.authority_epoch):
            return False
        return self.show_clock.sample(
            server_time=server_time, sent_monotonic=sent_monotonic,
            received_monotonic=received_monotonic,
        ) in ("anchored", "kept")

    async def probe_time(self) -> bool:
        if self.registration is None:
            raise Unauthorized("not_registered")
        sent_monotonic = self.clock.monotonic()
        body, received_monotonic = await self._request_with_receipt(
            "GET", "/v1/player/time", time=True
        )
        sample = PlayerTime.model_validate(body)

        def apply() -> bool:
            self._main()
            return self._sample_show_clock(sample.player_id, sample.authority_epoch,
                                           sample.server_time, sent_monotonic, received_monotonic)

        return await self.dispatch(apply)

    async def _time_loop(self):
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self.probe_time()
            except MainLoopLate:
                self._main_loop_late()
            except Exception as error:
                # A failed time request is named like any other; it never gates a plan or a
                # photo, since every pushed state also samples the show clock.
                self._request_failed(error)
            await asyncio.sleep(max(0, 1 - (asyncio.get_running_loop().time() - started)))

    async def _control_loop(self):
        """Every FEEDBACK_INTERVAL: the held plan's feedback (readiness and the downloads still
        wanted) and its readiness post, only when readiness changed or its post went unanswered
        (observations are `_observation_loop`'s). It asks Central for nothing: the state arrives
        over the session socket (`_websocket_loop`). A failed request is a fault and the next turn
        tries again (decision 0032, `_request_failed`), so downloads follow the held plan through
        an outage."""
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                readiness = await self.dispatch(self._feedback)
            except MainLoopLate:
                # Nothing is posted: the readiness Central holds stays (E-G8-8 decision).
                self._main_loop_late()
                await asyncio.sleep(max(0, FEEDBACK_INTERVAL - (asyncio.get_running_loop().time() - started)))
                continue
            numbered = None if readiness is None else (readiness.authority_epoch, readiness.sequence)
            if numbered is not None and numbered != self._readiness_answered:
                # Sent once per change (the executor numbers it once), until Central answers it.
                try:
                    await self.request("POST", "/v1/player/readiness", body=readiness.model_dump(mode="json"))
                    self._readiness_answered = numbered
                except StaleFeedback:
                    # A 409 is not always a newer plan: Central also answers it while a variant
                    # is made again (`media_unavailable`) or a lock conflicts, and those clear by
                    # themselves. So it stays unanswered and goes again next turn; a newer plan
                    # arrives over the socket and its readiness is numbered anew (a refused one
                    # writes nothing).
                    pass
                except REQUEST_FAILURES as error:
                    # Central answered and refused it (any other 4xx: its authority, binding or
                    # shape): judged, so not sent again; else it is sent again next turn with its
                    # number, which Central writes once.
                    if isinstance(error, Refused) and 400 <= error.status < 500:
                        self._readiness_answered = numbered
                    self._request_failed(error)
            await asyncio.sleep(max(0, FEEDBACK_INTERVAL - (asyncio.get_running_loop().time() - started)))

    async def _observation_loop(self):
        """Send the outbox's reports, each until Central answers it. A slow observation endpoint
        cannot consume the readiness reporting slot, and never holds a picture: the main loop only
        puts into the outbox."""
        while not self._stop.is_set():
            observation = self._reports.take()
            if observation is None:
                await asyncio.sleep(.05)
                continue
            if self._observation_current(observation):
                try:
                    await self.request("POST", "/v1/player/observations",
                                       body=observation.model_dump(mode="json"))
                except StaleFeedback:
                    # Central offered a newer revision meanwhile: sent again under it once the
                    # Player holds it (`receive_state`, or already; the executor reports a change
                    # once, never again).
                    self._reports.stale(observation)
                    self._renew_stale_reports()
                except REQUEST_FAILURES as error:
                    # Decision 0032: tried again at the next turn, with its number, so a Frame's
                    # shown history survives an outage and Central writes it once however often
                    # it is sent; a refusal Central judged (4xx) is final.
                    judged = isinstance(error, Refused) and 400 <= error.status < 500
                    if not judged:
                        self._reports.again(observation)
                    self._request_failed(error)
                    if not judged:
                        await asyncio.sleep(FEEDBACK_INTERVAL)

    async def _websocket_loop(self):
        """The session socket: Central pushes the state on it first, then whenever it may have
        changed; the Player's one source of its state (decision 0034), and its liveness while open
        (central/infra/player_contact_store.py). Its close ends the session cycle.

        Central pushes a state once, when its delivery changes, so a state whose dispatch misses
        the deadline (MainLoopLate: never started, never applied) is held and tried again every
        FEEDBACK_INTERVAL until the GLib loop takes it; a newer push replaces it."""
        opening = self.clock.monotonic()
        async with self.link.websocket(self.session, "/v1/player/session",
                                       max_size=MAX_JSON) as socket:
            messages = aiter(socket)
            arriving = asyncio.ensure_future(anext(messages))
            held: tuple[dict, float] | None = None
            try:
                while True:
                    await asyncio.wait((arriving,), timeout=None if held is None else FEEDBACK_INTERVAL)
                    if arriving.done():
                        try:
                            message = arriving.result()
                        except StopAsyncIteration:
                            break
                        received = self.clock.monotonic()
                        arriving = asyncio.ensure_future(anext(messages))
                        body = _json(message)
                        if body.pop("type", None) != "state":
                            raise ServiceError("message_type")
                        held = (body, received)
                    try:
                        await self.receive_state(held[0], opening, held[1])
                        held = None
                    except MainLoopLate:
                        self._main_loop_late()
            finally:
                if not arriving.done():
                    arriving.cancel()
            raise ServiceError("session_closed")

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
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
                    await self.hello_protocol()
                    # The state arrives over the session socket, pushed first on its open, and
                    # anchors the show clock too, so a failing time route never holds the plan
                    # back. The held plan's downloads and feedback start at once (decision 0032),
                    # and a late GLib loop does not end the session: its loops raise
                    # main_loop_late.
                    session_started = self._loop.time()
                    tasks = [asyncio.create_task(self._websocket_loop()),
                             asyncio.create_task(self._control_loop()),
                             asyncio.create_task(self._time_loop()),
                             asyncio.create_task(self._media_loop()),
                             asyncio.create_task(self._observation_loop())]
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
                        self._acknowledged = None
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
                    # Decision 0032: the held plan's downloads outlive the session; the next
                    # `_feedback` rebuilds them from that plan (`_offered` follows Central's state).
                    await self.link.aclose()
                if not self._stop.is_set():
                    await asyncio.sleep(BACKOFF[min(attempt, 3)])
                    attempt = min(attempt + 1, 3)
        except asyncio.CancelledError:
            pass
        finally:
            # stop() cancels this task, and that cancel may land after run() already saw the stop
            # and left its loop, inside this cleanup: shielded, so it never surfaces to run()'s
            # caller nor cuts the cleanup short.
            closing = asyncio.ensure_future(self._close_run())
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    pass
            closing.result()

    async def _close_run(self) -> None:
        """run()'s end: the clients and the cache closed."""
        with self._lock:
            self._acknowledged = None
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

    def frames_black(self) -> tuple[FrameBlack, ...] | None:
        """Which Frames are black while content is wanted (GLib thread: the probe's answer). None
        with no executor: this Player cannot tell, which is never the same as nothing black."""
        return None if self.executor is None else self.executor.frames_black()

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
    ProbeResponder(dispatcher, frames_black=service.frames_black,
                   gl_renderer=lambda: root.renderer.gl_renderer).start()
    try:
        with contextlib.suppress(KeyboardInterrupt):
            loop.run()
    finally:
        root.close()


if __name__ == "__main__":
    main()
