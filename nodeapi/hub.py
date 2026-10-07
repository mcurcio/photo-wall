"""Central's side of one Node, per pipe: the NodeLink (E3b design §7.2, §8, §9.1, §9.3; E3d runs it).

A NodeLink holds one client in the Node's hub account and one pipe. It finds what the Node has from
the streams themselves (names, then each description with no options: never a reply that grows with
stream state, X4), drains every event and state stream of its pipe through cursor readers from
Central's own cursors, and calls and discovers the pipe's components. Every read item is committed
with its cursor in one store transaction: a gap row (a count, or "unknown" when an epoch ended), the
raw record keyed (stream, epoch, sequence) with its message id, then the cursor. A crash mid-commit
resumes from the store's cursor; a repeat is idempotent. Central records before it judges.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

import nats.errors
from nats.js.errors import APIError, NotFoundError

from contracts.node_link import CENTRAL_WRITER, METHOD_TOKEN, NODE_DOMAIN, STORE_LINES, Pipe
from nodeapi.buffers import BIRTH_KEY, PIPE_KEY, Role, role_of
from nodeapi.envelope import caller_headers
from nodeapi.epoch import Token, epoch_of
from nodeapi.pull import Batch, CursorReader, Gap, Read, StreamAbsent

if TYPE_CHECKING:
    from nats.aio.client import Client

log = logging.getLogger(__name__)


class LinkStore(Protocol):
    async def cursors(self) -> Mapping[str, Token]: ...           # every stream this Node and pipe has a cursor for

    async def commit(self, stream: str, batch: Batch) -> None: ...
        # one transaction: gap rows first, raw records keyed (stream, epoch, seq) with their message id,
        # then batch.cursor (None: the stream's cursor is removed); a repeat is idempotent

    async def action(self, kind: str, body: Mapping[str, object]) -> None: ...   # the action log


DRAIN_BATCH: Final = 64
DRAIN_TIMEOUT_SECONDS: Final = 1.0
_BACKOFF: Final = (0.1, 2.0)          # first and largest wait while the link is down
_REQUEST_SECONDS: Final = 5.0
_DISCOVER_SECONDS: Final = .5
_SERVICE_ERROR: Final = "Nats-Service-Error-Code"   # nats.micro's error reply header


class NodeLink:
    """One Node, one pipe: reconcile, drain, call, discover."""

    def __init__(self, client: Client, pipe: Pipe, store: LinkStore) -> None:
        self._client = client
        self._pipe = Pipe(pipe)
        self._store = store
        self._jetstream = client.jetstream(domain=NODE_DOMAIN)
        self._readers: dict[str, CursorReader] = {}
        self._state_streams: set[str] = set()

    async def reconcile(self) -> Mapping[str, str]:
        """Stream -> epoch for every events and state stream of this pipe. A cursor whose stream is
        gone is committed as Gap(epoch, seq, None) with no cursor; a reader is kept per stream."""
        epochs: dict[str, str] = {}
        for name in await self._stream_names():
            try:
                info = await self._jetstream.stream_info(name)
            except NotFoundError:
                continue
            role = role_of(info.config)
            if role not in (Role.EVENTS, Role.STATE) or info.config.metadata.get(PIPE_KEY) != self._pipe.value:
                continue
            epochs[name] = epoch_of(info)
            if role is Role.STATE:
                self._state_streams.add(name)
        cursors = await self._store.cursors()
        for stream, cursor in cursors.items():
            if stream not in epochs:
                await self._store.commit(stream, Batch((Gap(cursor.epoch, cursor.seq, None),), None, False))
        for stream in [stream for stream in self._readers if stream not in epochs]:
            await self._readers.pop(stream).close()
        for stream in epochs:
            if stream not in self._readers:
                self._readers[stream] = CursorReader(self._client, stream, cursors.get(stream), domain=NODE_DOMAIN)
        return MappingProxyType(epochs)

    async def run(self, stop: asyncio.Event) -> None:
        """Reconcile, then drain every stream until `stop` or the client closes. Reconcile again when
        a batch was `recreated`, a stream went absent, or a new `birth` revision was drained. A
        store error propagates (Central's process is the thing that failed); a link that is down is
        retried with backoff."""
        delay = _BACKOFF[0]
        try:
            while not stop.is_set() and not self._client.is_closed:
                try:
                    await self.reconcile()
                except (nats.errors.Error, asyncio.TimeoutError) as error:
                    log.info("reconcile: %r", error)
                    await _pause(stop, delay)
                    delay = min(delay * 2, _BACKOFF[1])
                    continue
                delay = _BACKOFF[0]
                if not self._readers:   # nothing attached yet: look again
                    await _pause(stop, _BACKOFF[1])
                    continue
                again = asyncio.Event()
                drains = [asyncio.create_task(self._drain(stream, reader, stop, again))
                          for stream, reader in self._readers.items()]
                try:
                    await asyncio.gather(*drains)
                finally:
                    for drain in drains:
                        drain.cancel()
                    await asyncio.gather(*drains, return_exceptions=True)
        finally:
            if not self._client.is_closed:
                for reader in self._readers.values():
                    await reader.close()

    async def _drain(self, stream: str, reader: CursorReader, stop: asyncio.Event, again: asyncio.Event) -> None:
        """Read and commit until `stop` or `again`; each read finishes with its commit, so a reader
        never stands past what the store holds except across a store error."""
        committed, delay = reader.cursor, _BACKOFF[0]
        while not stop.is_set() and not again.is_set():
            try:
                batch = await reader.read(DRAIN_BATCH, timeout=DRAIN_TIMEOUT_SECONDS)
            except StreamAbsent:
                again.set()
                return
            except (nats.errors.Error, asyncio.TimeoutError) as error:
                if self._client.is_closed:
                    return
                log.info("drain %s: %r", stream, error)
                await _pause(stop, delay)
                delay = min(delay * 2, _BACKOFF[1])
                continue
            delay = _BACKOFF[0]
            if batch.items or batch.cursor != committed:
                await self._store.commit(stream, batch)
                committed = batch.cursor
            born = stream in self._state_streams and any(
                isinstance(item, Read) and item.subject.rsplit(".", 1)[-1] == BIRTH_KEY for item in batch.items)
            if batch.recreated or born:
                again.set()

    async def call(self, component: str, method: str, payload: bytes, *, timeout: float) -> bytes:
        """Call `<component>.method.<method>` as Central; the outcome goes to the action log."""
        line = STORE_LINES.get(component)
        if line is None or line.pipe is not self._pipe:
            raise ValueError("call_outside_pipe")
        body = {"component": component, "method": method}
        try:
            reply = await self._client.request(f"{component}.{METHOD_TOKEN}.{method}", payload, timeout=timeout,
                                               headers=caller_headers(CENTRAL_WRITER))
        except nats.errors.Error as error:
            await self._store.action("call", {**body, "outcome": type(error).__name__})
            raise
        code = (reply.headers or {}).get(_SERVICE_ERROR)
        await self._store.action("call", {**body, "outcome": "ok" if code is None else f"error {code}"})
        return reply.data

    async def discover(self) -> list[Mapping[str, object]]:
        """Every instance's $SRV.INFO answer for this pipe's components, into the action log."""
        services: list[Mapping[str, object]] = []
        for line in STORE_LINES.values():
            if line.pipe is self._pipe:
                services += await self._gather(f"$SRV.INFO.{line.name}")
        await self._store.action("discover", {"services": services})
        return services

    async def _stream_names(self) -> list[str]:
        names: list[str] = []
        while True:
            reply = await self._client.request(f"$JS.{NODE_DOMAIN}.API.STREAM.NAMES",
                                               json.dumps({"offset": len(names)}).encode(), timeout=_REQUEST_SECONDS)
            answer = json.loads(reply.data)
            if "error" in answer:
                raise APIError.from_error(answer["error"])
            page = answer.get("streams") or []
            names += page
            if not page or len(names) >= answer.get("total", 0):
                return names

    async def _gather(self, subject: str) -> list[Mapping[str, object]]:
        """Every reply to one request within _DISCOVER_SECONDS, on a wildcard inbox (the hub lets
        Central subscribe to no literal one)."""
        inbox = self._client.new_inbox()
        subscription = await self._client.subscribe(inbox + ".*")
        replies = []
        try:
            await self._client.publish(subject, b"", reply=inbox + ".r")
            deadline = time.monotonic() + _DISCOVER_SECONDS
            while (left := deadline - time.monotonic()) > 0:
                try:
                    message = await subscription.next_msg(timeout=left)
                except nats.errors.TimeoutError:
                    break
                with contextlib.suppress(ValueError):
                    replies.append(json.loads(message.data))
        finally:
            await subscription.unsubscribe()
        return replies


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
