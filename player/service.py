"""Single-process Player control plane; renderer calls stay on the GLib thread."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import queue
import re
import secrets
import signal
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Awaitable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import ConfigDict, Field, model_validator

from contracts.clock_record import ClockRecord
from contracts.enrollment import OutputReport
from contracts.equipment import READ_CAP, equipment_device_id
from contracts.liveness import REPORT_INTERVAL, SESSION_BACKOFF
from contracts.models import (
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
from contracts.player_control import ControlAck, ControlHello, ControlSelection
from contracts.time import Clock, SystemClock, TimeMapping
from player.cache import Cache
from player.central_link import CentralLink, Session, read_refusal
from player.executor import AuthorityError, Executor
from player.identity import Identity, load_identity
from player.local_app_proof import LocalAppProofClient, LocalProofError
from player.output_discovery import discover_outputs, output_app_id
from player.rendering import CapacityResult, PrepareResult, PresentationResult, Renderer
from uplink import watchdog
from uplink.causes import Cause, UplinkError
from uplink.clock import RunClockRecord
from uplink.diagnosis import failure_text
from uplink.finder import CentralDiscovery, Found, find_central
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
# The session-cycle backoff is a liveness contract (Central's silence threshold is derived from
# it); the name is kept so tests can shorten the session retry.
BACKOFF = SESSION_BACKOFF
# A failed media download retries on its own schedule; it is not a liveness contract.
MEDIA_RETRY_BACKOFF = (1, 5, 15, 60)
LOCAL_PROOF_RETRY = 5.0
LOG = logging.getLogger("photo_wall.player")


class ServiceError(ValueError):
    """A bounded diagnostic code, with no response body or credential attached."""


class Unauthorized(ServiceError):
    pass


class StaleFeedback(ServiceError):
    pass


class PlayerConfig(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = Field(default=1, alias="schema")
    # The saved root (R1): written by provisioning only when its root came from mDNS, and
    # used only when the kernel command line names no Central. Opaque until saved_root():
    # any JSON value loads (no type or length check here), and only saved_root() -- called
    # only when the cmdline names no Central -- validates it, with the one Central-root
    # validator, uplink.origin.Origin.parse_root. So when the cmdline names Central, no
    # saved value of any type or length can stop the Player (U3).
    central_origin: Any = Field(default=None, repr=False)
    # The base tag this boot's diskless base was served, handed forward by the
    # appliance bootstrapper on the opt-in per-device path (0012 bead 6). When
    # set, the enrolled player reports it healthy on /v1/player/base-health so
    # the latest-verified frontier advances. Absent on the flashed/D0 baseline
    # and on the 0010 global `.deb` path -- no base-health is posted there.
    base_running_tag: str | None = Field(default=None, max_length=256)
    # A PEM bundle used instead of the Debian bundle for every Central request this
    # process makes (locate, httpx, websocket): main() builds the one Trust from it.
    ca_file: str | None = Field(default=None, max_length=4096)
    cache_dir: str | None = Field(default=None, max_length=4096)
    boot_context_file: str = Field(
        default="/run/photo-wall/boot.json", min_length=1, max_length=4096
    )
    cache_bytes: int = Field(default=512 * 1024**2, ge=1024**2, le=1024**4)
    decoder_limit: int = Field(default=4, ge=1, le=16)
    texture_budget: int = Field(default=512 * 1024**2, ge=1024**2, le=4 * 1024**3)
    # Accepted and ignored: R2 makes an http root legal. Kept because the model forbids
    # unknown keys and older handoffs and configs carry it.
    allow_http: bool = False

    @model_validator(mode="after")
    def trusted_origin(self):
        if self.cache_dir is not None and not Path(self.cache_dir).is_absolute():
            raise ValueError("absolute cache directory required")
        if not Path(self.boot_context_file).is_absolute():
            raise ValueError("absolute boot context path required")
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
    identify_output: IdentifyOutput | None = None
    identify_expires_at: Instant | None = None
    delivery_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    delivery_sequence: int | None = Field(default=None, ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def coherent_delivery(self):
        if (self.delivery_id is None) != (self.delivery_sequence is None):
            raise ValueError("control_delivery_pair")
        if self.identify_expires_at is not None and self.identify_output is None:
            raise ValueError("identify_expiry_without_request")
        if (self.delivery_sequence is not None and self.identify_output is not None
                and self.identify_expires_at is None):
            raise ValueError("identify_request_without_expiry")
        return self


class BootContext(Model):
    schema_version: Literal[2] = Field(alias="schema")
    # None means no boot ticket was issued for this boot -- the signal the
    # player and central key ticketless enroll and boot-health reporting on
    # (see enroll(), _report_boot_health, and central/registry.py enroll()).
    # A netboot (D1) boot always carries a real ticket here
    # (appliance/bootstrap.py boot()); hardware_boot_context() (D0/flashed)
    # never had a ticket and now says so honestly instead of synthesizing one.
    ticket_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{48}$")
    device_id: str = Field(pattern=r"^device-[a-f0-9]{64}$")
    boot_id: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    release_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    trial: bool
    # "volatile": netboot (D1), written by appliance/bootstrap.py boot().
    # "persistent": flashed (D0), synthesized by hardware_boot_context()
    # below when no netboot boot context file is present.
    persistence: Literal["volatile", "persistent"]
    fault: None = None


class Registration(Model):
    player_id: str = Field(pattern=r"^p-[a-f0-9]{32}$")
    token: str = Field(min_length=32, max_length=256, repr=False)
    authority_epoch: int = Field(ge=1)


class Challenge(Model):
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: Instant


def load_boot_context(path: Path) -> BootContext:
    with path.open("rb") as stream:
        return BootContext.model_validate(_json(stream.read(MAX_JSON + 1)))


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


def display_serial(raw: bytes | None) -> str | None:
    """Only put a bounded hardware serial on the local diagnostic display."""
    if raw is None:
        return None
    try:
        serial = raw.decode("ascii").strip(" \t\r\n\x00").lower()
    except UnicodeDecodeError:
        return None
    return serial if re.fullmatch(r"[a-f0-9]{8,32}", serial) else None


def hardware_boot_context(serial_reader: Callable[[], bytes | None] = read_pi_serial) -> BootContext:
    """Flashed/D0 fallback for when no netboot boot context file exists.

    Derives `device_id` from the Pi hardware serial using the SAME kind+hash
    scheme as the netboot bootstrap (appliance/bootstrap.py LinuxOps.device_id,
    kind="pi", via the shared contracts.equipment.equipment_device_id), so one
    Pi keeps one device_id across tiers (0008: device_id is the immutable
    serial).

    There is no netboot-ticket source at D0 (there is no boot server to
    issue one), so `ticket_id` is honestly `None` here -- this is the
    explicit "no boot ticket" signal `enroll()` and `_report_boot_health`
    key off of (re-keyed off the ticket itself, not `persistence`): it
    skips the netboot release-binding entirely and enrolls the player
    unbound (pending) by serial alone, with no prior boot ticket required.
    Boot-health / OS-release tracking has no ticket to report against at D0
    and remains out of scope here.
    """
    try:
        raw = serial_reader()
    except OSError as error:
        raise ServiceError("boot_equipment_identity") from error
    device_id = equipment_device_id("pi", raw) if raw is not None else None
    if device_id is None:
        raise ServiceError("boot_equipment_identity")
    return BootContext(
        schema=2,
        ticket_id=None,
        device_id=device_id,
        boot_id=str(uuid.uuid4()),
        release_id=secrets.token_hex(32),
        trial=False,
        persistence="persistent",
        fault=None,
    )


def resolve_boot_context(path: Path, *,
                         serial_reader: Callable[[], bytes | None] = read_pi_serial) -> BootContext:
    """D1 (netboot): the boot server writes `path` before handing off to the
    player service; use it verbatim, unmodified. D0 (flashed): no such file
    exists, so derive an equivalent boot context from the Pi hardware serial.
    """
    if path.exists():
        return load_boot_context(path)
    return hardware_boot_context(serial_reader)


@dataclass(frozen=True)
class Download:
    epoch: int
    plan_id: str
    revision: int
    layer: Layer


class GLibDispatcher:
    """At most four queued callbacks; caller awaits each, never blocks GLib."""

    def __init__(self, glib):
        self.glib = glib
        self._slots = threading.BoundedSemaphore(4)

    def __call__(self, callback: Callable) -> Future:
        future = Future()
        if not self._slots.acquire(blocking=False):
            future.set_exception(ServiceError("dispatch_capacity"))
            return future

        def run():
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(callback())
                    except Exception as error:
                        future.set_exception(error)
            finally:
                self._slots.release()
            return False

        self.glib.idle_add(run)
        return future


class _ChunkBridge:
    def __init__(self, authorized: Callable[[], bool]):
        self.queue = queue.Queue(maxsize=4)
        self.stopped = threading.Event()
        self.authorized = authorized

    async def put(self, chunk: bytes | None):
        while not self.stopped.is_set() and self.authorized():
            try:
                self.queue.put_nowait(chunk)
                return
            except queue.Full:
                await asyncio.sleep(.005)
        raise ServiceError("download_cancelled")

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
        raise ServiceError("download_cancelled")


class PlayerService:
    def __init__(self, config: PlayerConfig, identity: Identity,
                 outputs: tuple[OutputReport, ...], renderer: Renderer, dispatcher: Callable,
                 *, find_central: Callable[[], Awaitable[Found]], trust: Trust,
                 clock: Clock | None = None, client: httpx.AsyncClient | None = None,
                 time_client: httpx.AsyncClient | None = None,
                 clock_record: Callable[[], ClockRecord | None] = RunClockRecord().read,
                 websocket_connect=None, cache_factory=Cache, executor_factory=Executor,
                 app_proof_client: LocalAppProofClient | None = None,
                 health_path: Path | None = Path("/run/photo-wall/player/service-health.json"),
                 boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
                 boot_context: BootContext | None = None):
        self.config, self.identity, self.outputs = config, identity, outputs
        self.renderer, self.dispatcher = renderer, dispatcher
        self.clock = clock or SystemClock()
        self.mapping = TimeMapping(self.clock)
        self.link = CentralLink(trust, unauthorized=Unauthorized, client=client,
                                time_client=time_client, websocket_connect=websocket_connect)
        self.clock_record = clock_record
        self.cache_factory, self.executor_factory = cache_factory, executor_factory
        self.app_proof_client = (LocalAppProofClient() if app_proof_client is None
                                 else app_proof_client)
        self.health_path = health_path
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
        self._proof_active = False
        self._proof_generation = 0
        self._thread = None
        self._loop = None
        self._task = None
        self.last_fault: str | None = None
        self.last_fault_detail: str | None = None
        # The authority epoch whose base-health has been reported (0012 bead 6);
        # None until a check-in lands, then one report per epoch (a re-enroll
        # bumps the epoch and re-reports; central is monotonic per epoch).
        self._base_health_epoch: int | None = None
        self._base_health_sequence_epoch: int | None = None
        self._base_health_sequence = 0
        self._hello_epoch: int | None = None
        self._control_selection: ControlSelection | None = None
        self._highest_control_sequence = 0
        self._highest_control_delivery_id: str | None = None
        self.central_link_state: Literal["connecting", "reachable", "retrying"] = "connecting"
        self._configuration_received = False
        self._unbound_outputs = tuple(output.output_id for output in outputs if output.connected)
        self._identify_key: tuple[str, int] | None = None
        self._identify_output: str | None = None
        self._identify_deadline: float | None = None

    def fault(self, code: str, *, detail: str | None = None):
        if self.last_fault != code:
            LOG.warning("player fault: %s", code if detail is None else f"{code} {detail}")
        self.last_fault, self.last_fault_detail = code, detail

    def _render_unbound_diagnostic(self) -> None:
        """Publish Central-link context only on known unbound outputs."""
        self._main()
        registration = self.registration
        self.renderer.set_unbound_outputs(
            self._unbound_outputs,
            registration.player_id if registration is not None else None,
            self.central_link_state,
            self._configuration_received,
        )

    def _set_central_link_state(self, state: Literal["connecting", "reachable", "retrying"]):
        self._main()
        self.central_link_state = state
        self._render_unbound_diagnostic()

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
        return await asyncio.wrap_future(self.dispatcher(callback))

    async def _mark_central_retrying(self) -> None:
        """Best-effort UI update; never let a diagnostic dispatch replace a cycle error."""
        try:
            await self.dispatch(lambda: self._set_central_link_state("retrying"))
        except Exception as error:
            LOG.debug("player: could not publish Central retry state: %s", type(error).__name__)

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
        """await find_central() (R1: cmdline root, else the saved root, else mDNS); keeps the
        result for the session. A located origin other than the one that issued the
        registration drops it (Session.relocated), so the next step re-enrolls there.
        Raises UplinkError."""
        found = await self._find_central()
        self._session = (Session(found.central) if self._session is None
                         else self._session.relocated(found.central))
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
        if self.boot_context is None:
            raise ServiceError("boot_context")
        session = self.session
        challenge = Challenge.model_validate(await self.request("POST", "/v1/enrollment/challenge",
            body={"public_key": self.identity.public_key}, authenticated=False))
        # A boot context with no ticket (D0/flashed, and 0009's diskless
        # bootstrapper) has no `appliance_devices` row to bind against, so
        # the presence of a ticket -- not `persistence` -- is what tells
        # central this is a ticketless enroll (0008 baseline).
        ticket_id = self.boot_context.ticket_id
        enrollment = self.identity.enrollment(
            challenge.nonce,
            self.outputs,
            device_id=self.boot_context.device_id,
            boot_id=self.boot_context.boot_id,
            ticket_id=ticket_id,
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

                await self.dispatch(create)
            except Exception as error:
                if self.cache is not None:
                    await asyncio.get_running_loop().run_in_executor(self._worker, self.cache.close)
                self.cache = self.executor = None
                self._session = session.unregistered()
                raise ServiceError("player_initialization") from error

    async def hello_protocol(self) -> None:
        """Negotiate before either state transport; only this hello's 404 means legacy."""
        if self.registration is None:
            raise Unauthorized("not_registered")
        epoch = self.registration.authority_epoch
        if self._hello_epoch == epoch:
            return
        offer = ControlHello(authority_epoch=epoch, schemas=(1, 2),
                             capabilities=("identify_output",))
        try:
            response = await self.request("POST", "/v1/player/hello",
                                          body=offer.model_dump(mode="json"))
        except UplinkError as error:
            if error.cause is Cause.HTTP and error.detail == "status=404":
                self._control_selection = ControlSelection(authority_epoch=epoch, schema=1)
                self._hello_epoch = epoch
                return
            raise
        selected = ControlSelection.model_validate(response)
        if selected.authority_epoch != epoch or selected.schema_version not in offer.schemas:
            raise ServiceError("control_selection_invalid")
        if any(capability not in offer.capabilities for capability in selected.capabilities):
            raise ServiceError("control_selection_invalid")
        self._control_selection = selected
        self._hello_epoch = epoch

    async def _report_base_health(self):
        """One-shot base-image health check-in after enroll (0012 bead 6).

        The diskless base is served per-device by tag; the appliance
        bootstrapper hands the tag it booted forward as
        `PlayerConfig.base_running_tag` (public.json). Once enrolled, the player
        reports that tag healthy on `POST /v1/player/base-health` -- an
        enrolled-token endpoint that requires NO live plan offer, so even an
        unbound device advances the latest-verified frontier. Central validates
        `running_tag == devices.last_served_tag` (the tag it recorded serving
        this device this boot), so the reported tag is the served tag, never a
        guess. Best-effort and idempotent per authority epoch: a failed or
        repeated check-in never disturbs the coordination loop -- it retries on
        the next enroll. A player with no handed-forward tag (flashed/D0 or the
        0010 global path) reports nothing.
        """
        tag = self.config.base_running_tag
        if not tag or self.registration is None:
            return
        if self._base_health_epoch == self.registration.authority_epoch:
            return
        epoch = self.registration.authority_epoch
        if self._base_health_sequence_epoch != epoch:
            self._base_health_sequence_epoch = epoch
            self._base_health_sequence = 0
        self._base_health_sequence += 1
        try:
            response = await self.request("POST", "/v1/player/base-health", body={
                "authority_epoch": epoch,
                "sequence": self._base_health_sequence,
                "running_tag": tag,
                "healthy": True,
            })
            if response.get("accepted") is not True:
                raise ServiceError("base_health_rejected")
            self._base_health_epoch = epoch
        except (ServiceError, Unauthorized, StaleFeedback, httpx.HTTPError,
                UplinkError, asyncio.TimeoutError) as error:
            LOG.info("player base-health check-in deferred: %s",
                     error if isinstance(error, (ServiceError, UplinkError))
                     else type(error).__name__)

    def _apply_state(self, state: State):
        self._main()
        if self.registration is None or (
            state.configuration.player_id != self.registration.player_id
            or state.configuration.authority_epoch != self.registration.authority_epoch
        ):
            raise ServiceError("state_authority")
        sequence = state.delivery_sequence
        if sequence is None:
            # An old pod can answer during a mixed rollout. Its unsequenced state
            # cannot supersede a v2 delivery already observed in this epoch.
            if self._highest_control_sequence:
                return "superseded"
        elif sequence < self._highest_control_sequence:
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
            bound = {binding.output_id for binding in configuration.bindings}
            self._unbound_outputs = tuple(output.output_id for output in self.outputs
                                          if output.connected and output.output_id not in bound)
            self._configuration_received = True
            self.central_link_state = "reachable"
            self._render_unbound_diagnostic()
            unbound = self._unbound_outputs
            selection = self._control_selection
            cue = state.identify_output if (
                sequence is not None and selection is not None
                and selection.authority_epoch == configuration.authority_epoch
                and selection.schema_version == 2
                and "identify_output" in selection.capabilities
            ) else None
            deadline = None
            if cue is not None:
                try:
                    deadline = self.mapping.deadline(state.identify_expires_at)
                except ValueError:
                    cue = None
            self._apply_identify_output(cue, unbound,
                                        configuration.authority_epoch, deadline=deadline)
            self.tick_main()
            return "rejected" if rejected else "applied"

    async def _ack_control(self, state: State, result: str) -> None:
        selection = self._control_selection
        if (result == "superseded" or state.delivery_id is None
                or self.registration is None or selection is None
                or selection.schema_version != 2
                or selection.authority_epoch != self.registration.authority_epoch):
            return
        report = ControlAck(authority_epoch=self.registration.authority_epoch,
                            delivery_id=state.delivery_id, result=result)
        # A superseded delivery can return accepted=false; the next state retries with a
        # fresh challenge. This is not plan readiness or OS management evidence.
        await self.request("POST", "/v1/player/control-acks",
                           body=report.model_dump(mode="json"))

    def _apply_identify_output(self, cue: IdentifyOutput | None,
                               unbound: tuple[str, ...], authority_epoch: int,
                               *, deadline: float | None = None) -> None:
        now = self.clock.monotonic()
        key = None if cue is None else (cue.request_id, cue.authority_epoch)
        if (cue is None or cue.authority_epoch != authority_epoch
                or cue.output_id not in unbound
                or (deadline is not None and deadline <= now)):
            self._identify_key = None
            self._identify_output = None
            self._identify_deadline = None
        else:
            if key != self._identify_key:
                self._identify_key = key
                self._identify_deadline = min(
                    now + min(cue.remaining_seconds, 15.0),
                    deadline if deadline is not None else math.inf,
                )
            if self._identify_deadline is not None and now >= self._identify_deadline:
                self._identify_output = None
                self._identify_deadline = None
            elif self._identify_deadline is not None:
                self._identify_output = cue.output_id
        self._sync_identify_output()

    def _sync_identify_output(self) -> None:
        self.renderer.set_identify_output(self._identify_output)

    def tick_main(self):
        self._main()
        if self._identify_deadline is not None and self.clock.monotonic() >= self._identify_deadline:
            self._identify_output = None
            self._identify_deadline = None
            self._sync_identify_output()
        if self.executor is not None:
            self.executor.prepare_imminent()
            observations = self.executor.tick()
            with self._lock:
                for observation in observations:
                    if len(self._observations) == self._observations.maxlen:
                        self.fault("observation_capacity")
                    self._observations.append(observation)

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

    def _health(self):
        return self._health_status()[0]

    def _health_status(self):
        """One renderer-thread evaluation owns both health and its public reason."""
        self._main()
        if self.executor is None:
            return False, "executor"
        if self._configuration is None:
            return False, "configuration"
        if not self.mapping.healthy():
            return False, "clock"
        if not self.renderer.capacity(()).available:
            return False, "renderer_capacity"
        return True, "healthy"

    def _write_health(self, healthy: bool, reason: str | None = None):
        if self.health_path is None:
            return
        if reason is None:
            reason = "healthy" if healthy else "disconnected"
        if healthy and not self.boot_id:
            healthy, reason = False, "identity"
        if (reason not in {"healthy", "executor", "identity", "configuration", "clock",
                           "renderer_capacity", "disconnected"}
                or (reason == "healthy") != bool(healthy)):
            raise ValueError("inconsistent health reason")
        body = dict(boot_id=self.boot_id, sampled_monotonic=self.clock.monotonic(),
                    player_id=self.registration.player_id if self.registration else None,
                    authority_epoch=self.registration.authority_epoch if self.registration else None,
                    persistence="volatile", healthy=bool(healthy), health_reason=reason,
                    clock=asdict(self.mapping.diagnostics))
        temporary = None
        try:
            # The root-owned /run/photo-wall parent protects boot.json; this
            # Player-owned child is the only place health publication may write.
            fd, temporary = tempfile.mkstemp(prefix=".service-health-", dir=self.health_path.parent)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(body, stream, allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.health_path)
        except (OSError, ValueError):
            self.fault("health_storage")
        finally:
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)

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
        bridge = _ChunkBridge(lambda: self._authorized(job))
        worker = asyncio.get_running_loop().run_in_executor(self._worker,
            self.executor.acquire, job.layer.assignment_id, bridge.chunks())
        variant = job.layer.variant
        target = "/v1/media/" + variant.sha256
        try:
            async with self.link.stream(self.session, "GET", target, timeout=120) as response:
                await self._check_status(response, target)
                if (response.headers.get("content-type", "").split(";")[0] != variant.media_type
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise ServiceError("media_type")
                if self._length(response, variant.size) != variant.size:
                    raise ServiceError("media_length")
                size = 0
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    size += len(chunk)
                    if worker.done():
                        # Cache may have verified an existing exact blob without
                        # consuming bytes; it owns this independent success gate.
                        return worker.result()
                    if size > variant.size:
                        raise ServiceError("media_length")
                    await bridge.put(chunk)
                if size != variant.size:
                    raise ServiceError("media_length")
                await bridge.put(None)
                return await asyncio.shield(worker)
        finally:
            # No HTTP failure/cancellation leaves the worker blocked on queue.get.
            bridge.stopped.set()
            await asyncio.shield(worker)

    async def _media_loop(self):
        attempts: dict[Download, tuple[int, float]] = {}
        maintained_at, verified_at = -math.inf, -math.inf
        while not self._stop.is_set():
            with self._lock:
                jobs = self._jobs
            attempts = {job: value for job, value in attempts.items() if job in jobs}
            for job in jobs:
                count, retry_at = attempts.get(job, (0, 0))
                if self.clock.monotonic() < retry_at or not self._authorized(job):
                    continue
                try:
                    success = await self.download(job)
                except Unauthorized:
                    raise
                except UplinkError as error:
                    if error.cause is Cause.REDIRECT:
                        raise           # ends the cycle: run() locates again (0014)
                    success = False
                    self._uplink_fault("media_download", error)
                except (httpx.HTTPError, ServiceError, TimeoutError, OSError, AuthorityError):
                    success = False
                    self.fault("media_download")
                attempts[job] = (min(count + 1, 3), self.clock.monotonic()
                    + (1 if success else MEDIA_RETRY_BACKOFF[min(count, 3)]))
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

    async def poll_state(self):
        body, _ = await self._request_with_receipt("GET", "/v1/player/state")
        state = State.model_validate(body)
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
                self._uplink_fault("clock_probe", error)
            except (httpx.HTTPError, ServiceError, TimeoutError, ValueError):
                self.fault("clock_probe")
            await asyncio.sleep(max(0, 1 - (asyncio.get_running_loop().time() - started)))

    async def _control_loop(self):
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            await self.poll_state()
            readiness, observations = await self.dispatch(self._feedback)
            healthy, reason = await self.dispatch(self._health_status)
            # The signed-release boot-health trial has been retired (0009):
            # every boot is ticketless, so there is no per-boot release to
            # report against and no watchdog to disarm.
            self._write_health(healthy, reason)
            if readiness is not None:
                try:
                    await self.request("POST", "/v1/player/readiness", body=readiness.model_dump(mode="json"))
                except StaleFeedback:
                    await self.poll_state()
            for observation in observations:
                if len(self._outgoing) == self._outgoing.maxlen:
                    self.fault("observation_capacity")
                self._outgoing.append(observation)
            # A completed state/readiness exchange with the GLib thread answering: the
            # session is live (M5; the unit's WatchdogSec is the deadline owner).
            watchdog.pet()
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
                    await self.poll_state()

    async def _websocket_loop(self):
        async with self.link.websocket(self.session, "/v1/player/session",
                                       max_size=MAX_JSON) as socket:
            async for message in socket:
                body = _json(message)
                if body.pop("type", None) != "state":
                    raise ServiceError("message_type")
                state = State.model_validate(body)
                result = await self.dispatch(lambda: self._apply_state(state))
                await self._ack_control(state, result)
            raise ServiceError("session_closed")

    async def _local_app_proof_loop(self) -> None:
        """Keep OS-local evidence separate from Central liveness and rendering.

        Only the process-local enrollment key enters the blocking socket worker.
        A re-enrollment or relocation replaces the Session object and invalidates
        that worker before it signs or sends a response.
        """
        recorded_session: Session | None = None
        while self._proof_active and not self._stop.is_set():
            session = self._session
            registration = session.registration if session is not None else None
            generation = self._proof_generation
            if (registration is not None and session is not recorded_session
                    and self.boot_context is not None and self.boot_id):
                def current() -> bool:
                    return (self._proof_active and self._proof_generation == generation
                            and not self._stop.is_set() and self._session is session
                            and session.registration is registration)

                try:
                    outcome = await asyncio.to_thread(
                        self.app_proof_client.exchange, identity=self.identity,
                        player_id=registration.player_id,
                        authority_epoch=registration.authority_epoch,
                        device_id=self.boot_context.device_id,
                        kernel_boot_id=self.boot_id,
                        enrollment_current=current,
                    )
                    if outcome == "recorded":
                        recorded_session = session
                        LOG.debug("player: OS-local app proof recorded")
                except (LocalProofError, OSError, TimeoutError) as error:
                    LOG.debug("player: OS-local app proof unavailable: %s",
                              str(error) if isinstance(error, LocalProofError)
                              else type(error).__name__)
                except Exception as error:
                    LOG.warning("player: OS-local app proof failed: %s", type(error).__name__)
            await asyncio.sleep(LOCAL_PROOF_RETRY)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self.link.open()
        self._proof_generation += 1
        self._proof_active = True
        proof_task = asyncio.create_task(self._local_app_proof_loop())
        attempt = 0
        try:
            while not self._stop.is_set():
                tasks = []
                session_started = None
                try:
                    if not self._located:
                        await self.locate_central()
                    if self.registration is None:
                        await self.enroll()
                        await self._report_base_health()
                    await self.hello_protocol()
                    # Reconnection reconciles authority before any download work.
                    await self.probe_time()
                    await self.poll_state()
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
                    self.fault("registration_required")
                    if not self._stop.is_set():
                        await self._mark_central_retrying()
                except Exception as error:
                    self._located = False
                    self._fault_for(error)
                    if not self._stop.is_set():
                        await self._mark_central_retrying()
                finally:
                    if session_started is not None and self._loop.time() - session_started >= 30:
                        attempt = 0
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    with self._lock:
                        self._offered = False
                        self._jobs = ()
                    self._write_health(False)
                    watchdog.pet()      # one completed cycle, whatever its outcome (M5)
                if not self._stop.is_set():
                    await asyncio.sleep(BACKOFF[min(attempt, 3)])
                    attempt = min(attempt + 1, 3)
        except asyncio.CancelledError:
            pass
        finally:
            self._proof_active = False
            self._proof_generation += 1
            proof_task.cancel()
            await asyncio.gather(proof_task, return_exceptions=True)
            await self.link.aclose()
            if self.cache is not None:
                await asyncio.get_running_loop().run_in_executor(self._worker, self.cache.close)
            self._worker.shutdown(wait=True, cancel_futures=True)
            self._session, self._located = None, False

    def start(self):
        self._main()
        if self._thread is not None:
            raise RuntimeError("service already started")
        self._thread = threading.Thread(target=lambda: asyncio.run(self.run()),
                                        name="player-network", daemon=False)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)

    @property
    def stopped(self):
        return self._thread is not None and not self._thread.is_alive()


class UnavailableRenderer:
    """Registration remains possible without falsely acknowledging native output."""

    def prepare(self, layer):
        return PrepareResult("failed", "capacity")

    def set_unbound_outputs(self, output_ids, player_id, central_link_state,
                            configuration_received):
        pass

    def set_identify_output(self, output_id):
        pass

    def capacity(self, compositions):
        return CapacityResult(False)

    def present(self, composition):
        return PresentationResult("failed", "capacity")

    def release(self, assignment_id):
        pass


def central_finder(config: PlayerConfig, resolution: Configured | Unconfigured, *,
                   transport: Transport) -> Callable[[], Awaitable[Found]]:
    """R1/U3's wiring for the Player (design §2.3): the cmdline root wins outright, and then
    the saved root (config.central_origin) is never parsed or validated, only noted as
    ignored; without a cmdline root the saved root is validated (UplinkError when it is not a
    Central root) and used; mDNS is built only when resolution is Unconfigured and nothing is
    saved. The zeroconf import is deferred to that branch, so `import player.service` never
    pulls it in."""
    saved: Origin | None = None
    discovery: CentralDiscovery | None = None
    if isinstance(resolution, Configured):
        if config.central_origin is not None:
            LOG.info("player: the saved central_origin is ignored: the kernel command line "
                     "names %s", resolution.root)
    else:
        saved = config.saved_root()
        if saved is None:
            from player.mdns_discovery import MdnsCentralDiscovery

            discovery = MdnsCentralDiscovery()
    return functools.partial(find_central, resolution, transport=transport, saved=saved,
                             discovery=discovery)


def build_finder(config: PlayerConfig, *, transport: Transport,
                 cmdline_path: Path | None = None) -> Callable[[], Awaitable[Found]]:
    """main()'s pre-GTK construction step: read the kernel command line at `cmdline_path`
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
    boot_context = resolve_boot_context(Path(config.boot_context_file))
    discovery = discover_outputs()
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib

    from player.native import NativeOutput, NativeRenderer

    renderer = UnavailableRenderer()
    native_fault = None
    connected_outputs = tuple(output for output in discovery.outputs if output.connected)
    if connected_outputs:
        try:
            renderer = NativeRenderer(tuple(NativeOutput(output.output_id,
                output_app_id(output.output_id), output.width_px or 1920,
                output.height_px or 1080) for output in connected_outputs),
                decoder_limit=config.decoder_limit, texture_budget=config.texture_budget,
                serial=display_serial(read_pi_serial()))
        except Exception:
            native_fault = "native_initialization"
    service = PlayerService(
        config,
        identity,
        discovery.outputs,
        renderer,
        GLibDispatcher(GLib),
        boot_context=boot_context,
        find_central=find,
        trust=trust,
    )
    for fault in (discovery.fault, native_fault):
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
            if hasattr(renderer, "close"):
                renderer.close()
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
    GLib.timeout_add(33, tick)
    service.start()
    watchdog.ready()    # Type=notify: started; WatchdogSec runs from here (M5)
    with contextlib.suppress(KeyboardInterrupt):
        loop.run()


if __name__ == "__main__":
    main()
