"""The Node-role session: one component's way to use its bus (E3b design §7.2 node, §9.3, §9.5).

A session runs its own thread and event loop and connects forever. Every connect runs **attach**:
apply the component's slice, re-put the state it holds plus `birth`, register its methods once as a
`nats.micro` service named the component (subjects `<component>.method.<name>`), then mark attached.
A bus that started empty is refilled so, by the writer that owns the data.

The handles never wait on the bus: `Events.emit` enqueues into a bounded outbox (full, it drops its
oldest) whose events carry headers built once at the emit, so a retried publish keeps its message id
and the server dedupes it; `State.put` keeps the latest value per key and publishes it. Both publish
only while attached, in order, retrying until the bus acknowledges. Each handled method call emits
`<line>.record.call` naming its caller. The raw client is never exposed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final

import nats
import nats.errors
import nats.micro
from nats.micro.service import ServiceConfig

from contracts.node_link import METHOD_TOKEN
from nodeapi.buffers import BIRTH_KEY, OUTBOX_KEY, Role, Slice, apply, role_of, table_of
from nodeapi.documents import header_bytes
from nodeapi.envelope import event_headers, writer_of

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MethodCall:
    method: str
    payload: bytes
    caller: str                         # the request's writer header; "unknown" when absent


MethodHandler = Callable[[MethodCall], Awaitable[bytes]]


@dataclass(frozen=True)
class Release:
    version: str                        # semantic version: the method service's version
    digest: str
    schema_majors: Mapping[str, int]


OUTBOX_MESSAGES: Final = 4096
SESSION_SCHEMA_MAJOR: Final = 1         # the major of the session's own events and birth
_UNKNOWN_CALLER: Final = "unknown"
_BACKOFF: Final = (0.1, 2.0)            # first and largest wait between attempts
_PUBLISH_SECONDS: Final = 2.0
_COMPONENT = re.compile(r"[a-z]+")


class NodeSession:
    """One component's session on its Node's bus (its line's declarer in S1)."""

    def __init__(self, component: str, slice_: Slice, release: Release, *, url: str,
                 methods: Mapping[str, MethodHandler] | None = None) -> None:
        if type(component) is not str or not _COMPONENT.fullmatch(component):
            raise ValueError("session_component")
        if slice_.store_line.declarer != component:
            raise ValueError("session_line_declared_by_another")
        self._methods = dict(methods or {})
        self._configs = {config.name: config for config in slice_.buffers}
        line = slice_.line
        calls = slice_.captures(f"{line}.record.call")
        if self._methods and (calls is None or role_of(self._configs[calls]) is not Role.EVENTS):
            raise ValueError("session_methods_need_records")
        self._service_config = ServiceConfig(name=component, version=release.version) if self._methods else None
        [state] = (config for config in slice_.buffers if role_of(config) is Role.STATE)
        self._state_table = table_of(state)
        self._state_prefix = state.subjects[0].removesuffix(">")
        self._birth = json.dumps({
            "component": component, "version": release.version, "pipe": slice_.store_line.pipe.value,
            "release_digest": release.digest, "schema_majors": dict(release.schema_majors),
            "slice_digest": slice_.digest}, sort_keys=True, separators=(",", ":")).encode()
        if len(self._birth) > self._state_table.sizes[BIRTH_KEY]:
            raise ValueError("state_value_too_large")
        self._component, self._slice, self._url = component, slice_, url
        self._lock = threading.Lock()
        self._outbox: deque[tuple[str, bytes, dict[str, str]]] = deque(maxlen=OUTBOX_MESSAGES)
        self._held: dict[str, bytes] = {}
        self._dirty: dict[str, None] = {}   # state keys put and not yet acknowledged, in put order
        self._attached = threading.Event()
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._events = Events(self)
        self._state = State(self)

    @property
    def events(self) -> Events:
        return self._events

    @property
    def state(self) -> State:
        return self._state

    def start(self) -> None:
        """Run the session on its own thread and loop; returns at once."""
        if self._thread is not None:
            raise RuntimeError("session_started")
        self._thread = threading.Thread(target=asyncio.run, args=(self._main(),),
                                        name=f"nodeapi-{self._component}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop_requested = True
            loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):   # the loop already ended
                loop.call_soon_threadsafe(self._stopping.set)
        if self._thread is not None:
            self._thread.join(timeout)

    def wait_attached(self, timeout: float) -> bool:
        return self._attached.wait(timeout)

    # The session's own loop.

    async def _main(self) -> None:
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()          # attached on the current connection
        self._attach_needed = asyncio.Event()
        self._service = None
        with self._lock:
            self._loop = asyncio.get_running_loop()
            if self._stop_requested:
                self._stopping.set()
        client = await self._connect()
        if client is None:
            return
        self._client, self._jetstream = client, client.jetstream()
        self._attach_needed.set()
        self._wake.set()
        tasks = [asyncio.create_task(self._attacher()), asyncio.create_task(self._publisher())]
        try:
            await self._stopping.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), _PUBLISH_SECONDS)
            self._attached.clear()

    async def _connect(self):
        delay = _BACKOFF[0]
        while not self._stopping.is_set():
            try:
                return await nats.connect(
                    servers=[self._url], allow_reconnect=True, max_reconnect_attempts=-1,
                    reconnect_time_wait=.5, connect_timeout=2, error_cb=self._on_error,
                    disconnected_cb=self._on_disconnected, reconnected_cb=self._on_reconnected)
            except (OSError, nats.errors.Error, asyncio.TimeoutError) as error:
                log.debug("%s: connect: %r", self._component, error)
                await self._pause(delay)
                delay = min(delay * 2, _BACKOFF[1])
        return None

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _on_error(self, error: Exception) -> None:
        log.debug("%s: %r", self._component, error)

    async def _on_disconnected(self) -> None:
        self._ready.clear()
        self._attached.clear()

    async def _on_reconnected(self) -> None:
        self._attach_needed.set()

    async def _attacher(self) -> None:
        while True:
            await self._attach_needed.wait()
            self._attach_needed.clear()
            delay = _BACKOFF[0]
            while True:
                try:
                    await self._attach()
                    break
                except Exception as error:   # the bus is away or restarting: attach again
                    log.info("%s: attach: %r", self._component, error)
                    await self._pause(delay)
                    delay = min(delay * 2, _BACKOFF[1])

    async def _attach(self) -> None:
        """Apply the slice, re-put every held state key and birth, register the methods once."""
        await apply(self._jetstream, self._slice)
        with self._lock:
            held = dict(self._held)
        for key, value in {**held, BIRTH_KEY: self._birth}.items():
            await self._jetstream.publish(self._state_prefix + key, value, timeout=_PUBLISH_SECONDS)
            self._done((self._state_prefix + key, value, None))   # a pending put of this value is made
        if self._service_config is not None and self._service is None:
            service = await nats.micro.add_service(self._client, config=self._service_config)
            try:
                for name, handler in self._methods.items():
                    await service.add_endpoint(name=name, subject=f"{self._component}.{METHOD_TOKEN}.{name}",
                                               handler=self._endpoint(name, handler))
            except BaseException:
                with contextlib.suppress(Exception):
                    await service.stop()
                raise
            self._service = service
        self._ready.set()
        self._attached.set()

    def _endpoint(self, name: str, handler: MethodHandler):
        async def answer(request) -> None:
            call = MethodCall(name, request.data, writer_of(request.headers) or _UNKNOWN_CALLER)
            try:
                reply = await handler(call)
            except Exception as error:
                log.exception("%s: method %s", self._component, name)
                outcome = "error"
                with contextlib.suppress(Exception):
                    await request.respond_error("500", type(error).__name__)
            else:
                outcome = "ok"
                await request.respond(reply)
            self._events.emit("record.call", json.dumps(
                {"method": name, "caller": call.caller, "outcome": outcome}, separators=(",", ":")).encode(),
                schema_major=SESSION_SCHEMA_MAJOR)
        return answer

    async def _publisher(self) -> None:
        """Publish held state and the outbox in order while attached, retrying until acknowledged."""
        delay = _BACKOFF[0]
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                await self._ready.wait()
                item = self._next()
                if item is None:
                    break
                subject, payload, headers = item
                try:
                    await self._jetstream.publish(subject, payload, headers=headers, timeout=_PUBLISH_SECONDS)
                except Exception as error:   # kept, retried with its headers (and message id)
                    log.info("%s: publish %s: %r", self._component, subject, error)
                    await self._pause(delay)
                    delay = min(delay * 2, _BACKOFF[1])
                    continue
                delay = _BACKOFF[0]
                self._done(item)

    def _next(self) -> tuple[str, bytes, dict[str, str] | None] | None:
        with self._lock:
            if self._dirty:
                key = next(iter(self._dirty))
                return self._state_prefix + key, self._held[key], None
            return self._outbox[0] if self._outbox else None

    def _done(self, item: tuple[str, bytes, dict[str, str] | None]) -> None:
        subject, payload, headers = item
        with self._lock:
            if headers is None:   # a state put: done unless the key was put again meanwhile
                key = subject.removeprefix(self._state_prefix)
                if self._held.get(key) is payload:
                    self._dirty.pop(key, None)
            elif self._outbox and self._outbox[0] is item:
                self._outbox.popleft()

    # The handles' side, on any thread.

    def _notify(self) -> None:
        with self._lock:
            loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):   # the loop already ended
                loop.call_soon_threadsafe(self._wake.set)

    def _emit(self, subject: str, payload: bytes, schema_major: int) -> None:
        full = f"{self._slice.line}.{subject}"
        stream = self._slice.captures(full)
        if stream is None or role_of(self._configs[stream]) is not Role.EVENTS:
            raise ValueError("event_subject_not_in_slice")
        headers = event_headers(schema_major)
        if len(payload) + header_bytes(headers) > self._configs[stream].max_msg_size:
            raise ValueError("event_too_large")
        with self._lock:
            self._outbox.append((full, bytes(payload), headers))   # full: drops the oldest (S6 counts it)
        self._notify()

    def _put(self, key: str, value: bytes) -> None:
        if key in (BIRTH_KEY, OUTBOX_KEY):
            raise ValueError("state_key_reserved")
        if key not in self._state_table.sizes:
            raise ValueError("state_key_unlisted")
        if len(value) > self._state_table.sizes[key]:
            raise ValueError("state_value_too_large")
        with self._lock:
            self._held[key] = bytes(value)
            self._dirty.pop(key, None)
            self._dirty[key] = None
        self._notify()

    def _get(self, key: str) -> bytes | None:
        with self._lock:
            return self._held.get(key)


class Events:
    def __init__(self, session: NodeSession) -> None:
        self._session = session

    def emit(self, subject: str, payload: bytes, *, schema_major: int) -> None:
        """Enqueue one event on `<line>.<subject>`; thread-safe, never blocks or waits for an ack."""
        self._session._emit(subject, payload, schema_major)


class State:
    def __init__(self, session: NodeSession) -> None:
        self._session = session

    def put(self, key: str, value: bytes) -> None:
        """Hold `value` as the key's state and publish it; thread-safe, never blocks."""
        self._session._put(key, value)

    def get(self, key: str) -> bytes | None:
        """This session's own last put."""
        return self._session._get(key)
