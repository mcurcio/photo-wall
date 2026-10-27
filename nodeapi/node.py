"""A component's way to use its Node's bus: the Node-role session, a read-only WALL follower, and a
read-only follower of another component's state keys (`StateReader`).

Each runs on one lifecycle (`_Lifecycle`): their own thread and event loop, a connect that runs
forever, **attach** at every connect, and stop. Every store line's declarer is its own component
(`contracts.node_link.STORE_LINES`; 0017 C4, the Player its own line since 2026-10-16), so a session
is built only for the line its component declares, and its attach applies its slice; then re-puts
the state it holds plus `birth` (contracts/node_state.py `Birth`, the one home of its shape),
registers its methods once as a `nats.micro` service named the component (subjects
`<component>.method.<name>`), and marks attached. A session that reads the wall, and every
WallReader, also creates the Node's WALL mirror at attach when it is absent. Attach runs again on
every reconnect and whenever a publish finds a stream of the slice absent, so `birth` is written in
every epoch in any start order, and a bus that started empty is refilled by the writer that owns the
data.

The handles never wait on the bus: `Events.emit` enqueues into a bounded outbox (full, it drops its
oldest) whose events carry headers built once at the emit, so a retried publish keeps its message id
and the server dedupes it. The session holds state `outbox` = {"dropped": n}, the events the outbox
dropped since the session started, re-held whenever n changes and published once the outbox is empty,
so a loss on the Node reaches Central as one counted row. `State.put` keeps the latest value per key
and publishes it. Both publish only while attached, in order, retrying until the bus acknowledges;
every attach re-puts the held state. Each handled method call emits `<line>.record.call` naming its
caller. The raw client is never exposed.

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
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Final, Generic, TypeVar

import nats
import nats.errors
import nats.micro
from nats.aio.client import Client
from nats.js.errors import NoStreamResponseError
from nats.micro.service import ServiceConfig

from contracts.node_link import METHOD_TOKEN, WALL_STREAM
from contracts.node_state import Birth, encode_birth
from nodeapi.buffers import (
    BIRTH_KEY,
    OUTBOX_KEY,
    WALL_PREFIX,
    Role,
    Slice,
    apply,
    declare,
    desired_prefix,
    desired_stream,
    role_of,
    state_prefix,
    state_stream,
    table_of,
    wall_mirror_config,
)
from nodeapi.documents import Document, header_bytes
from nodeapi.envelope import MethodCall, MethodHandler, event_headers, writer_of
from nodeapi.pull import CursorReader, Read, StartAt

log = logging.getLogger(__name__)


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
_WATCH_BATCH: Final = 64
_WATCH_SECONDS: Final = 1.0
_COMPONENT = re.compile(r"[a-z]+")


class _Lifecycle:
    """One component's connection to its Node's bus: its own thread and event loop, a connect that runs
    forever (the client reconnects by itself once connected), `attach` at every connect and whenever
    asked, and stop. `attach(client, refresh)` is retried with backoff until it returns; `refresh` is
    True when it was asked for while already attached. `run(client)` gives the tasks that live as long
    as the connection does; they wait on `ready` to touch the bus. NodeSession, WallReader and StateReader each run
    on one."""

    def __init__(self, name: str, url: str, *, attach: Callable[[Client, bool], Awaitable[None]],
                 run: Callable[[Client], list[Coroutine[object, object, None]]]) -> None:
        self._name, self._url, self._attach, self._run = name, url, attach, run
        self._lock = threading.Lock()
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.attached = threading.Event()      # attached on the current connection, read on any thread
        self.ready = asyncio.Event()           # the same, for the loop's own tasks
        self._attach_needed = asyncio.Event()
        self._stopping = asyncio.Event()

    def start(self, thread_name: str) -> None:
        """Run on its own thread and loop; returns at once. RuntimeError("session_started") twice."""
        if self._thread is not None:
            raise RuntimeError("session_started")
        self._thread = threading.Thread(target=asyncio.run, args=(self._main(),), name=thread_name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float) -> None:
        """Stop and join the thread, at most `timeout` seconds."""
        with self._lock:
            self._stop_requested = True
        self.call_soon(self._stopping.set)
        if self._thread is not None:
            self._thread.join(timeout)

    def call_soon(self, callback: Callable[[], object]) -> None:
        """Run `callback` on the loop; thread-safe and never blocks. Nothing runs before the loop
        starts (its first turn does the work) or after it ends."""
        with self._lock:
            loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):   # the loop already ended
                loop.call_soon_threadsafe(callback)

    def request_attach(self) -> None:
        """Attach again, or refresh while attached; thread-safe."""
        self.call_soon(self._attach_needed.set)

    def lost(self) -> None:
        """On the loop: the bus no longer holds what attach made; attach again."""
        self.ready.clear()
        self.attached.clear()
        self._attach_needed.set()

    async def pause(self, seconds: float) -> None:
        """Wait `seconds`, or less once stopping."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _main(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            if self._stop_requested:
                self._stopping.set()
        client = await self._connect()
        if client is None:
            return
        self._attach_needed.set()
        tasks = [asyncio.create_task(self._attacher(client)),
                 *(asyncio.create_task(work) for work in self._run(client))]
        try:
            await self._stopping.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), _PUBLISH_SECONDS)
            self.ready.clear()
            self.attached.clear()

    async def _connect(self) -> Client | None:
        """Connect to the bus forever, with backoff; None once stopping."""
        async def on_error(error: Exception) -> None:
            log.debug("%s: %r", self._name, error)

        delay = _BACKOFF[0]
        while not self._stopping.is_set():
            try:
                return await nats.connect(
                    servers=[self._url], allow_reconnect=True, max_reconnect_attempts=-1,
                    reconnect_time_wait=.5, connect_timeout=2, error_cb=on_error,
                    disconnected_cb=self._on_disconnected, reconnected_cb=self._on_reconnected)
            except (OSError, nats.errors.Error, asyncio.TimeoutError) as error:
                log.debug("%s: connect: %r", self._name, error)
                await self.pause(delay)
                delay = min(delay * 2, _BACKOFF[1])
        return None

    async def _on_disconnected(self) -> None:
        self.ready.clear()
        self.attached.clear()

    async def _on_reconnected(self) -> None:
        self._attach_needed.set()

    async def _attacher(self, client: Client) -> None:
        while True:
            await self._attach_needed.wait()
            self._attach_needed.clear()
            refresh = self.ready.is_set()
            delay = _BACKOFF[0]
            while True:
                try:
                    await self._attach(client, refresh)
                    break
                except Exception as error:   # the bus is away or restarting: attach again
                    log.info("%s: attach: %r", self._name, error)
                    await self.pause(delay)
                    delay = min(delay * 2, _BACKOFF[1])
            if not refresh:
                self.ready.set()
                self.attached.set()


async def _watch(client: Client, stream: str, prefix: str, view: _View, value: Callable[[Read], object], *,
                 life: _Lifecycle, name: str, answered: Callable[[bool], None] | None = None) -> None:
    """Follow `stream` into `view` from each key's latest value, while attached. The reader keeps its
    own cursor and recreates its consumer itself; a stream absent or a bus away is read again after a
    pause. The view is never cleared: each key's last value stays until the key is written again.
    `answered` is told after each read whether it answered."""
    reader = CursorReader(client, stream, None, start=StartAt.LAST_PER_SUBJECT)
    delay = _BACKOFF[0]
    try:
        while True:
            await life.ready.wait()
            try:
                batch = await reader.read(_WATCH_BATCH, timeout=_WATCH_SECONDS)
            except Exception as error:   # the stream is absent or the bus away: read again
                log.debug("%s: watch %s: %r", name, stream, error)
                if answered is not None:
                    answered(False)
                await life.pause(delay)
                delay = min(delay * 2, _BACKOFF[1])
                continue
            delay = _BACKOFF[0]
            if answered is not None:
                answered(True)
            latest = {item.subject.removeprefix(prefix): value(item)
                      for item in batch.items if isinstance(item, Read)}
            if latest:
                view._changed(latest)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.close(), _PUBLISH_SECONDS)


def _wall_value(item: Read) -> bytes:
    return item.data


class NodeSession:
    """One component's session on its Node's bus: the declarer of its slice's line."""

    def __init__(self, component: str, slice_: Slice, release: Release, *, url: str,
                 methods: Mapping[str, MethodHandler] | None = None, reads_wall: bool = False) -> None:
        """ValueError("session_component") unless component matches [a-z]+;
        ValueError("session_does_not_declare_line") unless the slice's line is the component's."""
        if type(component) is not str or not _COMPONENT.fullmatch(component):
            raise ValueError("session_component")
        if slice_.store_line.declarer != component:
            raise ValueError("session_does_not_declare_line")
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
        self._desired_stream = desired_stream(line) if desired else None
        self._desired_prefix = desired_prefix(line)
        self._reads_wall = bool(reads_wall)
        self._state_table = table_of(state)
        self._state_prefix = state_prefix(line)
        self._birth = encode_birth(Birth(
            component=component, version=release.version, pipe=slice_.store_line.pipe.value,
            release_digest=release.digest, schema_majors=dict(release.schema_majors),
            slice_digest=slice_.digest))
        if len(self._birth) > self._state_table.sizes[BIRTH_KEY]:
            raise ValueError("state_value_too_large")
        self._component, self._slice, self._url = component, slice_, url
        self._lock = threading.Lock()
        self._outbox: deque[tuple[str, bytes, dict[str, str]]] = deque(maxlen=OUTBOX_MESSAGES)
        self._dropped = 0                   # events the full outbox dropped, never published
        self._held: dict[str, bytes] = {}
        self._dirty: dict[str, None] = {}   # state keys put and not yet acknowledged, in put order
        self._count_dropped(0)
        self._life = _Lifecycle(component, url, attach=self._attach, run=self._tasks)
        self._wake = asyncio.Event()
        self._service = None
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
        self._life.start(f"nodeapi-{self._component}")

    def stop(self, timeout: float = 5.0) -> None:
        self._life.stop(timeout)

    def wait_attached(self, timeout: float) -> bool:
        return self._life.attached.wait(timeout)

    # The session's own loop.

    def _tasks(self, client: Client) -> list[Coroutine[object, object, None]]:
        self._client, self._jetstream = client, client.jetstream()
        self._wake.set()
        tasks = [self._publisher()]
        if self._desired is not None:
            tasks.append(_watch(client, self._desired_stream, self._desired_prefix, self._desired,
                                lambda item: Document(item.data, writer_of(item.headers), item.token),
                                life=self._life, name=self._component))
        if self._wall is not None:
            tasks.append(_watch(client, WALL_STREAM, WALL_PREFIX, self._wall, _wall_value,
                                life=self._life, name=self._component))
        return tasks

    async def _attach(self, client: Client, refresh: bool) -> None:
        """Apply the slice; create the WALL mirror if this session reads it, re-put every held state
        key and birth, register the methods once. An attach asked for while already attached on this
        connection (a reconnect noticed twice) does nothing: putting the state again would only
        supersede values Central has not read yet."""
        if refresh:
            return
        await apply(self._jetstream, self._slice)
        if self._reads_wall:
            await declare(self._jetstream, wall_mirror_config())   # create only, never updated
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
                await self._life.ready.wait()
                item = self._next()
                if item is None:
                    break
                subject, payload, headers = item
                try:
                    await self._jetstream.publish(subject, payload, headers=headers, timeout=_PUBLISH_SECONDS)
                except NoStreamResponseError as error:   # a stream of the slice went absent: attach again
                    log.info("%s: publish %s: %r", self._component, subject, error)
                    self._life.lost()
                    continue
                except Exception as error:   # kept, retried with its headers (and message id)
                    log.info("%s: publish %s: %r", self._component, subject, error)
                    await self._life.pause(delay)
                    delay = min(delay * 2, _BACKOFF[1])
                    continue
                delay = _BACKOFF[0]
                self._done(item)

    def _next(self) -> tuple[str, bytes, dict[str, str] | None] | None:
        with self._lock:
            # The drop count waits for the outbox to empty, so an overflow costs no event throughput.
            key = next((key for key in self._dirty if key != OUTBOX_KEY or not self._outbox), None)
            if key is not None:
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
                self._count_dropped(-1)

    def _count_dropped(self, change: int) -> None:
        """Change the drop count and hold it as dirty state `outbox`; the caller holds the lock."""
        self._dropped += change
        self._held[OUTBOX_KEY] = json.dumps({"dropped": self._dropped}, separators=(",", ":")).encode()
        self._dirty[OUTBOX_KEY] = None

    # The handles' side, on any thread.

    def _notify(self) -> None:
        self._life.call_soon(self._wake.set)

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
                self._count_dropped(1)
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


class WallReader:
    """A read-only follower of the Node's WALL mirror, apart from any session (the Player reads the
    room-lights document through one, beside its own line's session). No slice,
    state, birth or methods. It runs on the session's lifecycle: at every connect it creates the WALL
    mirror when absent (create only, `wall_mirror_config`, as a session with reads_wall does), then
    follows WALL into `wall` from each key's latest value through the session's own watch. The view
    keeps each key's last value across a bus restart until the key is written again (last good stays)
    and calls its callbacks on the reader's thread. `start` returns at once; nothing here blocks its
    caller."""

    def __init__(self, component: str, *, url: str) -> None:
        """ValueError("session_component") unless component matches [a-z]+."""
        if type(component) is not str or not _COMPONENT.fullmatch(component):
            raise ValueError("session_component")
        self._component = component
        self._wall = WallView()
        self._answered = False              # the latest watch read on this attach answered
        self._life = _Lifecycle(component, url, attach=self._attach, run=self._tasks)

    @property
    def wall(self) -> WallView:
        """The Node's WALL mirror, read-only."""
        return self._wall

    def following(self) -> bool:
        """Whether the reader follows the mirror now: connected, the mirror there, and its latest
        read answered. False before the first, while the bus is away and while reads fail. Thread-safe;
        never blocks."""
        return self._life.attached.is_set() and self._answered

    def start(self) -> None:
        """Run on its own thread and loop; returns at once. RuntimeError("session_started") twice."""
        self._life.start(f"nodeapi-wall-{self._component}")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop and join the thread, at most `timeout` seconds."""
        self._life.stop(timeout)

    def _tasks(self, client: Client) -> list[Coroutine[object, object, None]]:
        return [_watch(client, WALL_STREAM, WALL_PREFIX, self._wall, _wall_value, life=self._life,
                       name=self._component, answered=self._read)]

    async def _attach(self, client: Client, refresh: bool) -> None:
        self._answered = False
        await declare(client.jetstream(), wall_mirror_config())   # create only, never updated

    def _read(self, answered: bool) -> None:
        self._answered = answered


class StateReader:
    """A read-only follower of another component's state bucket on the same Node's bus (Display Host
    reads the Player line's `impeded` through one, beside its own line's session). No slice, state,
    birth or methods, and nothing declared: the bucket is its declarer's, and while it is absent the
    reader reads again after a pause. It follows the bucket from each key's latest value through the
    session's own watch and keeps only `keys`, so a busy line's other keys cost one read, never a
    callback. The view keeps each key's last value until the key is written again (a bus restart
    clears nothing; the writer restates at its attach), and calls its callbacks on the reader's
    thread. `start` returns at once; nothing here blocks its caller."""

    def __init__(self, component: str, line: str, keys: frozenset[str], *, url: str) -> None:
        """ValueError("session_component") unless component matches [a-z]+."""
        if type(component) is not str or not _COMPONENT.fullmatch(component):
            raise ValueError("session_component")
        self._component, self._line, self._keys = component, line, frozenset(keys)
        self._state = _View[bytes]()
        self._life = _Lifecycle(component, url, attach=self._attach, run=self._tasks)

    @property
    def state(self) -> _View[bytes]:
        """The followed keys of the line's state bucket, read-only."""
        return self._state

    def start(self) -> None:
        """Run on its own thread and loop; returns at once. RuntimeError("session_started") twice."""
        self._life.start(f"nodeapi-state-{self._component}-{self._line}")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop and join the thread, at most `timeout` seconds."""
        self._life.stop(timeout)

    def _tasks(self, client: Client) -> list[Coroutine[object, object, None]]:
        prefix = state_prefix(self._line)

        def kept(item: Read) -> bytes | None:
            return item.data if item.subject.removeprefix(prefix) in self._keys else None

        return [_watch(client, state_stream(self._line), prefix, _Kept(self._state), kept,
                       life=self._life, name=self._component)]

    async def _attach(self, client: Client, refresh: bool) -> None:
        return None


class _Kept:
    """A view's `_changed` that drops the keys a StateReader does not follow (their value None)."""

    def __init__(self, view: _View[bytes]) -> None:
        self._view = view

    def _changed(self, latest: Mapping[str, bytes | None]) -> None:
        kept = {key: value for key, value in latest.items() if value is not None}
        if kept:
            self._view._changed(kept)


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
