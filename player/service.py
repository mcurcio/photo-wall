"""Single-process Player control plane; renderer calls stay on the GLib thread."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import queue
import re
import signal
import ssl
import tempfile
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import ConfigDict, Field, model_validator

from contracts.enrollment import OutputReport
from contracts.models import (
    Commit,
    Instant,
    Layer,
    Model,
    Observation,
    Plan,
    PlayerConfiguration,
    Revocation,
)
from contracts.time import Clock, SystemClock, TimeMapping
from player.cache import Cache
from player.executor import AuthorityError, Executor
from player.identity import Identity, load_identity
from player.output_discovery import discover_outputs, output_app_id
from player.rendering import CapacityResult, PrepareResult, PresentationResult, Renderer

MAX_JSON = 1024 * 1024
CHUNK_SIZE = 64 * 1024
BACKOFF = (1, 5, 15, 60)
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
    central_origin: str = Field(max_length=2048)
    ca_file: str | None = Field(default=None, max_length=4096)
    state_dir: str = Field(max_length=4096)
    cache_bytes: int = Field(default=512 * 1024**2, ge=1024**2, le=1024**4)
    decoder_limit: int = Field(default=4, ge=1, le=16)
    texture_budget: int = Field(default=512 * 1024**2, ge=1024**2, le=4 * 1024**3)
    allow_http: bool = False

    @model_validator(mode="after")
    def trusted_origin(self):
        try:
            parsed = urlsplit(self.central_origin)
            valid = (parsed.scheme in ("https", "http") and parsed.hostname
                     and parsed.port != 0 and not parsed.username and not parsed.password
                     and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in self.central_origin)
                     and "\\" not in self.central_origin)
        except ValueError:
            valid = False
        if not valid or (parsed.scheme == "http" and not self.allow_http):
            raise ValueError("one trusted HTTPS origin required")
        if not Path(self.state_dir).is_absolute():
            raise ValueError("absolute state directory required")
        if self.ca_file is not None and not Path(self.ca_file).is_absolute():
            raise ValueError("absolute public CA path required")
        return self


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
    server_time: Instant


class Registration(Model):
    player_id: str = Field(pattern=r"^p-[a-f0-9]{32}$")
    token: str = Field(min_length=32, max_length=256, repr=False)
    authority_epoch: int = Field(ge=1)


class Challenge(Model):
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: Instant


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
                 *, clock: Clock | None = None, client: httpx.AsyncClient | None = None,
                 websocket_connect=None, cache_factory=Cache, executor_factory=Executor,
                 health_path: Path | None = Path("/run/photo-wall/player/service-health.json"),
                 boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id")):
        self.config, self.identity, self.outputs = config, identity, outputs
        self.renderer, self.dispatcher = renderer, dispatcher
        self.clock = clock or SystemClock()
        self.mapping = TimeMapping(self.clock)
        self.client = client
        self.websocket_connect = websocket_connect
        self.cache_factory, self.executor_factory = cache_factory, executor_factory
        self.health_path = health_path
        try:
            with boot_id_path.open("r") as stream:
                boot_id = stream.read(37).strip()
            self.boot_id = boot_id if re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot_id) else None
        except OSError:
            self.boot_id = None
        self.cache = None
        self.executor = None
        self.registration: Registration | None = None
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
        self._thread = None
        self._loop = None
        self._task = None
        self.last_fault: str | None = identity.fault

    def fault(self, code: str):
        if self.last_fault != code:
            LOG.warning("player fault: %s", code)
        self.last_fault = code

    def _main(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Player control requires the renderer thread")

    async def dispatch(self, callback):
        return await asyncio.wrap_future(self.dispatcher(callback))

    @property
    def origin(self):
        return self.config.central_origin.rstrip("/")

    def _headers(self, authenticated=True):
        headers = {"Accept-Encoding": "identity"}
        if authenticated:
            if self.registration is None:
                raise Unauthorized("not_registered")
            headers["Authorization"] = "Bearer " + self.registration.token
        return headers

    async def request(self, method: str, path: str, *, body=None, authenticated=True) -> dict:
        value, _ = await self._request_with_receipt(method, path, body=body, authenticated=authenticated)
        return value

    async def _request_with_receipt(self, method: str, path: str, *, body=None,
                                    authenticated=True) -> tuple[dict, tuple[float, float]]:
        async with asyncio.timeout(15):
            async with self.client.stream(method, self.origin + path, json=body,
                    headers=self._headers(authenticated), follow_redirects=False) as response:
                self._status(response)
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

    @staticmethod
    def _status(response):
        if response.status_code == 401:
            raise Unauthorized("unauthorized")
        if response.status_code == 409:
            raise StaleFeedback("state_changed")
        if response.status_code != 200:
            raise ServiceError(f"http_{response.status_code}")

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
        challenge = Challenge.model_validate(await self.request("POST", "/v1/enrollment/challenge",
            body={"public_key": self.identity.public_key}, authenticated=False))
        enrollment = self.identity.enrollment(challenge.nonce, self.outputs)
        registered = Registration.model_validate(await self.request("POST", "/v1/enrollment/register",
            body=enrollment.model_dump(mode="json"), authenticated=False))
        expected = "p-" + hashlib.sha256(bytes.fromhex(self.identity.public_key)).hexdigest()[:32]
        if registered.player_id != expected:
            raise ServiceError("registration_identity")
        self.registration = registered
        with self._lock:
            self._offered = False
            self._jobs = ()
        self._outgoing.clear()
        if self.identity.persistence == "durable" and self.executor is None:
            try:
                self.cache = await asyncio.get_running_loop().run_in_executor(self._worker,
                    lambda: self.cache_factory(Path(self.config.state_dir) / "cache", self.config.cache_bytes))

                def create():
                    self._main()
                    self.executor = self.executor_factory(registered.player_id, self.cache,
                        self.renderer, self.clock, self.mapping,
                        Path(self.config.state_dir) / "execution.json")

                await self.dispatch(create)
            except Exception as error:
                if self.cache is not None:
                    await asyncio.get_running_loop().run_in_executor(self._worker, self.cache.close)
                self.cache = self.executor = None
                # Preserve the same proven public identity; the next enrollment
                # explicitly withdraws durable storage capability at central.
                self.identity = replace(self.identity, persistence="volatile", fault="identity_storage")
                self.registration = None
                raise ServiceError("identity_storage") from error

    def _apply_state(self, state: State, sample=None):
        self._main()
        if self.registration is None or (
            state.configuration.player_id != self.registration.player_id
            or state.configuration.authority_epoch != self.registration.authority_epoch
        ):
            raise ServiceError("state_authority")
        if sample is not None:
            uncertainty, utc, mono = sample
            drift = abs((self.clock.utc() - utc) - (self.clock.monotonic() - mono))
            age = self.clock.monotonic() - mono
            self.mapping.establish(uncertainty if drift <= .01 and 0 <= age <= 1 else 86400)
        if self.executor is None:
            return
        with self._lock:
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
                self._seen_commits.append(commit)
            self.tick_main()

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
        if self.identity.persistence != "durable":
            return False, "identity"
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
                    persistence=self.identity.persistence, healthy=bool(healthy), health_reason=reason)
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
        bridge = _ChunkBridge(lambda: self._authorized(job))
        worker = asyncio.get_running_loop().run_in_executor(self._worker,
            self.executor.acquire, job.layer.assignment_id, bridge.chunks())
        variant = job.layer.variant
        try:
            async with asyncio.timeout(120):
                async with self.client.stream("GET", self.origin + "/v1/media/" + variant.sha256,
                        headers=self._headers(), follow_redirects=False) as response:
                    self._status(response)
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
                except (httpx.HTTPError, ServiceError, TimeoutError, OSError, AuthorityError):
                    success = False
                    self.fault("media_download")
                attempts[job] = (min(count + 1, 3), self.clock.monotonic()
                    + (1 if success else BACKOFF[min(count, 3)]))
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
        before_utc, before_mono = self.clock.utc(), self.clock.monotonic()
        body, (after_utc, after_mono) = await self._request_with_receipt("GET", "/v1/player/state")
        state = State.model_validate(body)
        elapsed = after_mono - before_mono
        uncertainty = (elapsed / 2 + abs(state.server_time - (before_utc + elapsed / 2)))
        if (elapsed < 0 or not math.isfinite(uncertainty)
                or abs((after_utc - before_utc) - elapsed) > .01):
            uncertainty = 86400
        await self.dispatch(lambda: self._apply_state(state, (uncertainty, after_utc, after_mono)))

    async def _control_loop(self):
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            await self.poll_state()
            readiness, observations = await self.dispatch(self._feedback)
            self._write_health(*await self.dispatch(self._health_status))
            if readiness is not None:
                try:
                    await self.request("POST", "/v1/player/readiness", body=readiness.model_dump(mode="json"))
                except StaleFeedback:
                    await self.poll_state()
            for observation in observations:
                if len(self._outgoing) == self._outgoing.maxlen:
                    self.fault("observation_capacity")
                self._outgoing.append(observation)
            await asyncio.sleep(max(0, .5 - (asyncio.get_running_loop().time() - started)))

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
        from websockets.asyncio.client import connect

        connector = self.websocket_connect or connect
        scheme = "wss" if self.origin.startswith("https:") else "ws"
        uri = scheme + self.origin[self.origin.index(":"): ] + "/v1/player/session"
        options = dict(additional_headers=self._headers(), max_size=MAX_JSON, max_queue=4,
                       compression=None, proxy=None, open_timeout=15, close_timeout=3,
                       ping_interval=10, ping_timeout=10)
        if scheme == "wss":
            options["ssl"] = ssl.create_default_context(cafile=self.config.ca_file)
        async with connector(uri, **options) as socket:
            async for message in socket:
                body = _json(message)
                if body.pop("type", None) != "state":
                    raise ServiceError("message_type")
                state = State.model_validate(body)
                await self.dispatch(lambda: self._apply_state(state))
            raise ServiceError("session_closed")

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        owns_client = self.client is None
        if owns_client:
            self.client = httpx.AsyncClient(verify=ssl.create_default_context(cafile=self.config.ca_file),
                follow_redirects=False, trust_env=False, timeout=15,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))
        attempt = 0
        try:
            while not self._stop.is_set():
                tasks = []
                session_started = None
                try:
                    if self.registration is None:
                        await self.enroll()
                    # Reconnection reconciles authority before any download work.
                    await self.poll_state()
                    session_started = self._loop.time()
                    tasks = [asyncio.create_task(self._control_loop()),
                             asyncio.create_task(self._media_loop()),
                             asyncio.create_task(self._observation_loop())]
                    if self.websocket_connect is not False:
                        tasks.append(asyncio.create_task(self._websocket_loop()))
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    attempt = 0
                except Unauthorized:
                    self.registration = None
                    self.fault("registration_required")
                except Exception as error:
                    self.fault(str(error) if isinstance(error, ServiceError) else "connection_failed")
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
                if not self._stop.is_set():
                    await asyncio.sleep(BACKOFF[min(attempt, 3)])
                    attempt = min(attempt + 1, 3)
        except asyncio.CancelledError:
            pass
        finally:
            if owns_client:
                await self.client.aclose()
            if self.cache is not None:
                await asyncio.get_running_loop().run_in_executor(self._worker, self.cache.close)
            self._worker.shutdown(wait=True, cancel_futures=True)
            self.registration = None

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

    def capacity(self, compositions):
        return CapacityResult(False)

    def present(self, composition):
        return PresentationResult("failed", "capacity")

    def release(self, assignment_id):
        pass


def main():
    parser = argparse.ArgumentParser(description="Photo Wall Player")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config(args.config)
    identity = load_identity(Path(config.state_dir))
    discovery = discover_outputs()
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib

    from player.native import NativeOutput, NativeRenderer

    renderer = UnavailableRenderer()
    native_fault = None
    connected_outputs = tuple(output for output in discovery.outputs if output.connected)
    if identity.persistence == "durable" and connected_outputs:
        try:
            renderer = NativeRenderer(tuple(NativeOutput(output.output_id,
                output_app_id(output.output_id), output.width_px or 1920,
                output.height_px or 1080) for output in connected_outputs),
                decoder_limit=config.decoder_limit, texture_budget=config.texture_budget)
        except Exception:
            native_fault = "native_initialization"
    service = PlayerService(config, identity, discovery.outputs, renderer, GLibDispatcher(GLib))
    for fault in (discovery.fault, native_fault, identity.fault):
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
    with contextlib.suppress(KeyboardInterrupt):
        loop.run()


if __name__ == "__main__":
    main()
