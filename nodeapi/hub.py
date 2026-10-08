"""Central's side of one Node, per pipe, and of the wall: NodeLink and WallWriter (E3b design §7.2,
§8, §9.1-§9.3, §9.6, §10; E3d runs them).

A NodeLink holds one client in the Node's hub account and one pipe. It finds what the Node has from
the streams themselves (names, then each description with no options: never a reply that grows with
stream state, X4), drains every event and state stream of its pipe through cursor readers from
Central's own cursors, and calls and discovers the pipe's components. Every read item is committed
with its cursor in one store transaction: a gap row (a count, or "unknown" when an epoch ended), the
raw record keyed (stream, epoch, sequence) with its message id, then the cursor. A crash mid-commit
resumes from the store's cursor; a repeat is idempotent. Central records before it judges.

A NodeLink also asserts Central's documents into each desired bucket of its pipe, by diff against
Central's own last write per key (`LinkStore.own_tokens`): only a key whose projection changed, or
every key in an epoch Central has not written. Each put is conditional on Central's own token, or on
"absent" in a new epoch. A conflict re-reads the key: Central's own value (an earlier write whose
acknowledgement was lost) gives Central its token; any other writer's value is adopted as Central's
for that projection and logged (`document_adopted`), so Central never overwrites it until its
projection changes again (C16). A key the bucket's table does not list (rollout skew), or a value
past its size, is refused before sending and logged (`document_refused`); every reconcile tries it
again, so the release that lists it (a new `birth`) gets it. When a bucket's table changes in an
epoch Central has written, every key Central owns there is put again (an adopted key is left), so a
shrink never leaves a listed key without its value (§9.2).

The link reconciles when it comes up, when a reader is lost or its stream goes absent, when a new
`birth` is drained, and when the bus's stream names differ from those its last reconcile listed: a
line created after it is drained within about 2 s (erratum E-E3B-CC-1). `run_link` is the one way
Central runs a NodeLink: it owns the client (Central imports no NATS type), connects as Central's
user in the Node's account, and connects again until stopped.

WallWriter keeps WALL in the hub's wall account: absent (the hub restarted empty), it is created at
Central's mark + 1 + WALL_MARGIN and every wall document is put again. The mark is recorded after
each acknowledgement, so a crash between the two loses at most the writes in flight, which the
margin covers: no Node mirror is ever ahead of the new WALL's first sequence (X8).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

import nats
import nats.errors
from nats.js.errors import APIError, NotFoundError

from contracts.node_link import (
    CENTRAL_INBOX_PREFIX,
    CENTRAL_WRITER,
    METHOD_TOKEN,
    NODE_DOMAIN,
    STORE_LINES,
    WALL_STREAM,
    Pipe,
    central_user,
)
from nodeapi.buffers import BIRTH_KEY, PIPE_KEY, KeyTable, Role, declare, role_of, wall_config
from nodeapi.documents import ABSENT, Absent, Conflict, DocumentRefused, DocumentWriter
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

    async def own_tokens(self, stream: str) -> Mapping[str, tuple[str, Token]]: ...
        # key -> (digest, token) of Central's last write of each key of one desired bucket

    async def wrote(self, stream: str, key: str, digest: str, token: Token) -> None: ...


class DocumentSource(Protocol):
    async def documents(self, stream: str) -> Mapping[str, bytes]: ...   # Central's projection for one desired bucket


DRAIN_BATCH: Final = 64
DRAIN_TIMEOUT_SECONDS: Final = 1.0
_BACKOFF: Final = (0.1, 2.0)          # first and largest wait while the link is down
_NAMES_SECONDS: Final = _BACKOFF[1]   # how often a running link lists the bus's stream names
_REQUEST_SECONDS: Final = 5.0
_DISCOVER_SECONDS: Final = .5
_SERVICE_ERROR: Final = "Nats-Service-Error-Code"   # nats.micro's error reply header


class NodeLink:
    """One Node, one pipe: reconcile, drain, assert, call, discover."""

    def __init__(self, client: Client, pipe: Pipe, store: LinkStore, documents: DocumentSource) -> None:
        self._client = client
        self._pipe = Pipe(pipe)
        self._store = store
        self._documents = documents
        self._jetstream = client.jetstream(domain=NODE_DOMAIN)
        self._readers: dict[str, CursorReader] = {}
        self._state_streams: set[str] = set()
        self._listed: frozenset[str] = frozenset()        # the names the last reconcile listed
        self._tables: dict[str, tuple[str, str]] = {}      # desired bucket -> (epoch, table) last asserted against

    async def reconcile(self) -> Mapping[str, str]:
        """Stream -> epoch for every events and state stream of this pipe. A cursor whose stream is
        gone is committed as Gap(epoch, seq, None) with no cursor; a reader is kept per stream. Then
        every desired bucket of this pipe is asserted (a no-op for one Central is current in)."""
        epochs: dict[str, str] = {}
        desired: list[str] = []
        names = await self._stream_names()
        self._listed = frozenset(names)
        for name in names:
            try:
                info = await self._jetstream.stream_info(name)
            except NotFoundError:
                continue
            role = role_of(info.config)
            if (info.config.metadata or {}).get(PIPE_KEY) != self._pipe.value:
                continue
            if role is Role.DESIRED:
                desired.append(name)
            if role not in (Role.EVENTS, Role.STATE):
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
        for stream in desired:
            with contextlib.suppress(NotFoundError):   # pruned meanwhile: nothing to assert into
                await self.assert_documents(stream)
        return MappingProxyType(epochs)

    async def assert_documents(self, stream: str) -> None:
        """Put each of Central's documents for one desired bucket whose digest differs from Central's
        own last write in the stream's current epoch, conditional on that write's token, or on
        ABSENT when Central has none in this epoch. When the bucket's table differs from the one this
        link last asserted against in the same epoch, a key Central owns with an unchanged digest is
        put again too, unless its value is another writer's (adopted). A document the table refuses
        is not sent: `document_refused` is logged and the next reconcile tries it again."""
        writer = await DocumentWriter.bind(self._client, stream, writer=CENTRAL_WRITER, domain=NODE_DOMAIN)
        described = (writer.epoch, writer.table.encoded())
        last = self._tables.get(stream)
        changed = last is not None and last[0] == described[0] and last[1] != described[1]
        own = await self._store.own_tokens(stream)
        for key, value in (await self._documents.documents(stream)).items():
            digest = _digest(value)
            held = own.get(key)
            if held is not None and held[1].epoch == writer.epoch:
                expect: Token | Absent = held[1]
                if held[0] == digest:
                    if not changed:
                        continue
                    found = await writer.read(key)
                    if found is not None and found.writer != CENTRAL_WRITER:   # adopted: left as it is
                        continue
            else:
                expect = ABSENT
            try:
                await self._assert_one(writer, stream, key, value, digest, expect)
            except DocumentRefused as refused:   # rollout skew, or past a shrunk size
                await self._store.action("document_refused", {"stream": stream, "key": key, "reason": str(refused)})
        self._tables[stream] = described

    async def _assert_one(self, writer: DocumentWriter, stream: str, key: str, value: bytes, digest: str,
                          expect: Token | Absent) -> None:
        while True:
            try:
                token = await writer.put(key, value, expect=expect)
                break
            except Conflict:
                found = await writer.read(key)
            if found is None:   # nothing there (a new epoch under the write): write it fresh
                expect = ABSENT
            elif found.writer == CENTRAL_WRITER:   # Central's own write, its acknowledgement lost
                if _digest(found.value) == digest:
                    token = found.token
                    break
                expect = found.token
            else:   # another writer's value: Central's for this projection, flagged to the operator
                await self._store.wrote(stream, key, digest, found.token)
                await self._store.action("document_adopted", {"stream": stream, "key": key, "writer": found.writer})
                return
        await self._store.wrote(stream, key, digest, token)

    async def run(self, stop: asyncio.Event) -> None:
        """Reconcile, then drain every stream until `stop` or the client closes. Reconcile again when
        a batch was `recreated`, a stream went absent, a new `birth` revision was drained, or the
        bus's stream names changed. A store error propagates (Central's process is the thing that
        failed); a link that is down is retried with backoff."""
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
                drains = [asyncio.create_task(self._follow(stream, reader, stop, again))
                          for stream, reader in self._readers.items()]
                looking = asyncio.create_task(self._look(stop, again))
                try:
                    await asyncio.gather(*drains)
                finally:
                    for task in (*drains, looking):
                        task.cancel()
                    await asyncio.gather(*drains, looking, return_exceptions=True)
        finally:
            if not self._client.is_closed:
                for reader in self._readers.values():
                    await reader.close()

    async def _look(self, stop: asyncio.Event, again: asyncio.Event) -> None:
        """Set `again` once the bus's stream names differ from those the last reconcile listed: a line
        applied after it. Polled (one bounded STREAM.NAMES reply), since nothing on the Node may send
        toward the hub (erratum E-E3B-CC-1)."""
        while not stop.is_set() and not again.is_set():
            await _pause(stop, _NAMES_SECONDS)
            try:
                if frozenset(await self._stream_names()) != self._listed:
                    again.set()
            except (nats.errors.Error, asyncio.TimeoutError) as error:   # the drains see the link down too
                log.info("names: %r", error)

    async def _follow(self, stream: str, reader: CursorReader, stop: asyncio.Event, again: asyncio.Event) -> None:
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


async def run_link(url: str, serial: str, pipe: Pipe, store: LinkStore, documents: DocumentSource,
                   stop: asyncio.Event) -> None:
    """One Node's link on one pipe, as Central's user in the Node's hub account (central_user(serial),
    inboxes under CENTRAL_INBOX_PREFIX): connect, retrying with the NodeLink backoff until `stop`; run
    NodeLink(...).run(stop); connect again if the client closed; close the client on `stop`. The client
    reconnects forever. A store error propagates (Central's process is what failed); a hub that does not
    answer never raises out of here. Callers never see a nats type."""
    user = central_user(serial)

    async def logged(error: Exception) -> None:
        log.debug("link %s %s: %r", pipe, serial, error)

    delay = _BACKOFF[0]
    while not stop.is_set():
        try:
            client = await nats.connect(
                servers=[url], user=user, password=user, inbox_prefix=CENTRAL_INBOX_PREFIX,
                allow_reconnect=True, max_reconnect_attempts=-1, reconnect_time_wait=_BACKOFF[0],
                connect_timeout=2, error_cb=logged)
        except (OSError, nats.errors.Error, asyncio.TimeoutError) as error:
            log.info("link %s %s: connect: %r", pipe, serial, error)
            await _pause(stop, delay)
            delay = min(delay * 2, _BACKOFF[1])
            continue
        delay = _BACKOFF[0]
        try:
            await NodeLink(client, pipe, store, documents).run(stop)
        finally:
            with contextlib.suppress(nats.errors.Error, asyncio.TimeoutError, OSError):
                await asyncio.wait_for(client.close(), _REQUEST_SECONDS)
        await _pause(stop, _BACKOFF[0])   # the client closed under the link: connect again


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


WALL_MARGIN: Final = 1024   # K: more than the wall writes ever in flight between an ack and its mark


class WallMarks(Protocol):
    async def mark(self) -> int: ...                       # the highest WALL sequence Central recorded; 0 for none

    async def record_mark(self, seq: int) -> None: ...

    async def wall_documents(self) -> Mapping[str, bytes]: ...   # Central's wall documents, key -> value


class WallWriter:
    """Central's one writer of the hub's WALL, inside the wall table."""

    def __init__(self, client: Client, table: KeyTable, marks: WallMarks) -> None:
        wall_config(table, first_seq=1)   # ValueError("wall_table_over_budget") at construction
        self._client = client
        self._table = table
        self._marks = marks
        self._writer: DocumentWriter | None = None

    async def ensure(self) -> bool:
        """Create WALL at mark + 1 + WALL_MARGIN if it is absent and put every wall document again;
        True when this call created it. Create-if-absent is race-safe with another Central."""
        jetstream = self._client.jetstream()
        try:
            await jetstream.stream_info(WALL_STREAM)
            created = False
        except NotFoundError:
            first = await self._marks.mark() + 1 + WALL_MARGIN
            created = await declare(jetstream, wall_config(self._table, first_seq=first))
        self._writer = await DocumentWriter.bind(self._client, WALL_STREAM, writer=CENTRAL_WRITER)
        if created:
            for key, value in (await self._marks.wall_documents()).items():
                await self.put(key, value)
        return created

    async def put(self, key: str, value: bytes) -> int:
        """Write one wall document, admitted by the table; the mark is recorded after the ack. WALL
        has one writer, Central, so a conflict (another Central instance) re-reads and writes again."""
        if self._writer is None:
            raise RuntimeError("wall_writer_not_ensured")
        while True:
            found = await self._writer.read(key)
            try:
                token = await self._writer.put(key, value, expect=ABSENT if found is None else found.token)
                break
            except Conflict:
                continue
        await self._marks.record_mark(token.seq)
        return token.seq
