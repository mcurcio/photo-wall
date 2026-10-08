"""The Node-role session: one component's way to use its bus (E3b design §7.2 node, §9.3, §9.5).

A session runs its own thread and event loop and connects forever. Every connect runs **attach**:
apply every line this component declares (`contracts.node_link.STORE_LINES`: its own slice's line and
any line handed to `apply_line`, such as apps' Player line from the held Player releases, §7.3), or,
for a slice whose line another component declares, wait with backoff until every stream of the slice
exists; then re-put the state it holds plus `birth`, register its methods once as a `nats.micro`
service named the component (subjects `<component>.method.<name>`), and mark attached. A session that
reads the wall also creates the Node's WALL mirror at attach when it is absent. Attach runs again on
every reconnect and whenever a publish finds a stream of the slice absent, so `birth` is written in
every epoch in any start order, and a bus that started empty is refilled by the writer that owns the
data (§9.5, erratum E-E3B-R2-4).

The handles never wait on the bus: `Events.emit` enqueues into a bounded outbox (full, it drops its
oldest) whose events carry headers built once at the emit, so a retried publish keeps its message id
and the server dedupes it. Every attach puts state `outbox` = {"dropped": n}, the events the outbox
dropped since the session started, so a loss on the Node reaches Central as one counted row (§11
"outbox full"). `State.put` keeps the latest value per key and publishes it. Both publish
only while attached, in order, retrying until the bus acknowledges. Each handled method call emits
`<line>.record.call` naming its caller. The raw client is never exposed.

Two read-only views follow their streams through a cursor reader that starts at each key's latest
value (`pull.StartAt.LAST_PER_SUBJECT`): `desired`, the slice's desired bucket, written by Central and
any later local writer, and `wall`, the Node's WALL mirror. Each holds the latest value per key, kept
across a bus restart until the key is written again (an absent key is "unknown": last good stays),
and calls its callbacks on the session's thread.
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
from typing import Final, Generic, TypeVar

import nats
import nats.errors
import nats.micro
from nats.js.errors import NoStreamResponseError, NotFoundError
from nats.micro.service import ServiceConfig

from contracts.node_link import METHOD_TOKEN, WALL_STREAM
from nodeapi.buffers import (
    BIRTH_KEY,
    OUTBOX_KEY,
    WALL_PREFIX,
    Role,
    Slice,
    apply,
    declare,
    role_of,
    table_of,
    wall_mirror_config,
)
from nodeapi.documents import Document, header_bytes
from nodeapi.envelope import event_headers, writer_of
from nodeapi.pull import CursorReader, Read, StartAt

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
ATTACH_BACKOFF_SECONDS: Final = (0.1, 2.0)   # first and largest wait while a declarer has not applied
_PUBLISH_SECONDS: Final = 2.0
_WATCH_BATCH: Final = 64
_WATCH_SECONDS: Final = 1.0
_COMPONENT = re.compile(r"[a-z]+")


class NodeSession:
    """One component's session on its Node's bus: its line's declarer, or a session that waits for it."""

    def __init__(self, component: str, slice_: Slice, release: Release, *, url: str,
                 methods: Mapping[str, MethodHandler] | None = None, reads_wall: bool = False) -> None:
        if type(component) is not str or not _COMPONENT.fullmatch(component):
            raise ValueError("session_component")
        self._declares = slice_.store_line.declarer == component
        self._methods = dict(methods or {})
        self._configs = {config.name: config for config in slice_.buffers}
        line = slice_.line
        calls = slice_.captures(f"{line}.record.call")
        if self._methods and (calls is None or role_of(self._configs[calls]) is not Role.EVENTS):
            raise ValueError("session_methods_need_records")
        self._service_config = ServiceConfig(name=component, version=release.version) if self._methods else None
        [state] = (config for config in slice_.buffers if role_of(config) is Role.STATE)
        desired = [config for config in slice_.buffers if role_of(config) is Role.DESIRED]
        if len(desired) > 1:
            raise ValueError("session_one_desired_bucket")
        self._desired_stream = desired[0].name if desired else None
        self._desired_prefix = desired[0].subjects[0].removesuffix(">") if desired else ""
        self._reads_wall = bool(reads_wall)
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
        self._dropped = 0                   # events the full outbox dropped, never published
        self._held: dict[str, bytes] = {}
        self._lines: dict[str, Slice] = {}  # line -> the slice `apply_line` handed over, applied at every attach
        self._dirty: dict[str, None] = {}   # state keys put and not yet acknowledged, in put order
        self._attached = threading.Event()
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._events = Events(self)
        self._state = State(self)
        self._desired = DesiredView() if self._desired_stream is not None else None
        self._wall = WallView() if self._reads_wall else None

    @property
    def events(self) -> Events:
        return self._events

    @property
    def state(self) -> State:
        return self._state

    @property
    def desired(self) -> DesiredView | None:
        """The slice's desired bucket, read-only; None when the slice has none."""
        return self._desired

    @property
    def wall(self) -> WallView | None:
        """The Node's WALL mirror, read-only; None unless the session reads the wall."""
        return self._wall

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

    def apply_line(self, slice_: Slice) -> None:
        """Hold `slice_` as the slice of a line this component declares (not its own slice's line) and
        apply it now and at every later attach; thread-safe, never blocks. apps hands over the union of
        the held Player releases' slices during a hot-swap, then the new slice alone (§9.7)."""
        if slice_.store_line.declarer != self._component or slice_.line == self._slice.line:
            raise ValueError("session_does_not_declare_line")
        with self._lock:
            self._lines[slice_.line] = slice_
            loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):   # the loop already ended
                loop.call_soon_threadsafe(self._attach_needed.set)

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
        if self._desired is not None:
            tasks.append(asyncio.create_task(self._watch(
                self._desired_stream, self._desired_prefix, self._desired,
                lambda item: Document(item.data, writer_of(item.headers), item.token))))
        if self._wall is not None:
            tasks.append(asyncio.create_task(self._watch(WALL_STREAM, WALL_PREFIX, self._wall, lambda item: item.data)))
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
                    if self._ready.is_set():   # attached: `apply_line` handed over a line
                        await self._apply_lines()
                    else:
                        await self._attach()
                    break
                except Exception as error:   # the bus is away or restarting: attach again
                    log.info("%s: attach: %r", self._component, error)
                    await self._pause(delay)
                    delay = min(delay * 2, _BACKOFF[1])

    async def _attach(self) -> None:
        """Apply every line this component declares, or wait until its slice's declarer has; create
        the WALL mirror if this session reads it, re-put every held state key and birth, register the
        methods once."""
        await self._apply_lines()
        if self._declares:
            await apply(self._jetstream, self._slice)
        else:
            delay = ATTACH_BACKOFF_SECONDS[0]
            while missing := await self._missing():
                log.debug("%s: waiting for %s's declarer: %s absent", self._component, self._slice.line, missing)
                await self._pause(delay)
                delay = min(delay * 2, ATTACH_BACKOFF_SECONDS[1])
        if self._reads_wall:
            await declare(self._jetstream, wall_mirror_config())   # create only, never updated (§10)
        with self._lock:
            held = dict(self._held)
            dropped = json.dumps({"dropped": self._dropped}, separators=(",", ":")).encode()
        for key, value in {**held, OUTBOX_KEY: dropped, BIRTH_KEY: self._birth}.items():
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

    async def _apply_lines(self) -> None:
        with self._lock:
            lines = list(self._lines.values())
        for slice_ in lines:
            await apply(self._jetstream, slice_)

    async def _missing(self) -> str | None:
        """The first stream of the slice the bus does not hold (STREAM.INFO, no options), or None."""
        for config in self._slice.buffers:
            try:
                await self._jetstream.stream_info(config.name)
            except NotFoundError:
                return config.name
        return None

    async def _watch(self, stream: str, prefix: str, view: _View, value: Callable[[Read], object]) -> None:
        """Follow `stream` into `view` from each key's latest value, while attached. The reader keeps
        its own cursor and recreates its consumer itself; a stream absent or a bus away is read again
        after a pause."""
        reader = CursorReader(self._client, stream, None, start=StartAt.LAST_PER_SUBJECT)
        delay = _BACKOFF[0]
        try:
            while True:
                await self._ready.wait()
                try:
                    batch = await reader.read(_WATCH_BATCH, timeout=_WATCH_SECONDS)
                except Exception as error:   # the stream is absent or the bus away: read again
                    log.debug("%s: watch %s: %r", self._component, stream, error)
                    await self._pause(delay)
                    delay = min(delay * 2, _BACKOFF[1])
                    continue
                delay = _BACKOFF[0]
                latest = {item.subject.removeprefix(prefix): value(item)
                          for item in batch.items if isinstance(item, Read)}
                if latest:
                    view._changed(latest)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader.close(), _PUBLISH_SECONDS)

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
                except NoStreamResponseError as error:   # a stream of the slice went absent: attach again
                    log.info("%s: publish %s: %r", self._component, subject, error)
                    self._ready.clear()
                    self._attached.clear()
                    self._attach_needed.set()
                    continue
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
            else:   # dropped by a full outbox while its publish was in flight: published, not lost
                self._dropped -= 1

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
            if len(self._outbox) == OUTBOX_MESSAGES:   # full: the append drops the oldest
                self._dropped += 1
            self._outbox.append((full, bytes(payload), headers))
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


_Value = TypeVar("_Value")


class _View(Generic[_Value]):
    """The latest value per key of one stream, and the callbacks told of each change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, _Value] = {}
        self._callbacks: list[Callable[[str, _Value], None]] = []

    def get(self, key: str) -> _Value | None:
        with self._lock:
            return self._values.get(key)

    def on_change(self, callback: Callable[[str, _Value], None]) -> None:
        """Call `callback(key, value)` on the session's thread for each key's latest new value."""
        with self._lock:
            self._callbacks.append(callback)

    def _changed(self, latest: Mapping[str, _Value]) -> None:
        with self._lock:
            self._values.update(latest)
            callbacks = list(self._callbacks)
        for key, value in latest.items():
            for callback in callbacks:
                try:
                    callback(key, value)
                except Exception:
                    log.exception("view callback for %s", key)


class DesiredView(_View[Document]):
    """The session's desired bucket, read-only: each key's latest document, its writer and token."""


class WallView(_View[bytes]):
    """The Node's WALL mirror, read-only: each wall key's latest value (keys without `wall.`)."""
