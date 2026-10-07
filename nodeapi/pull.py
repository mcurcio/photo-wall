"""The one way to pull from a JetStream consumer: one byte budget per connection (E-W1-TD-3, -7).

nats-server closes a local client whose undelivered bytes pass `max_pending` (a slow consumer,
ns:server/client.go:2624-2636), which a component whose loop is busy while large batches arrive
reaches at once. The server queues for one pull request at most the request's `max_bytes`, and a
connection's open requests add up, so the cap is per connection, not per request: every request on
one client takes its `max_bytes` from that client's budget of PULL_MAX_BYTES, half the Node's
max_pending (contracts.node_link.NODE_MAX_PENDING, which the config test binds to node-bus.conf), and
gives it back once the request has ended. The other half is the connection's other traffic.
Concurrent pulls on one client then queue for the budget instead of adding up, so the bytes
requested from the server and not yet read by the client never pass PULL_MAX_BYTES, however many
tasks pull at once. A busy reader then slows the server's writes, never ends its connection
(`write_timeout: retry`). nats-py 2.16.0's `fetch` sends no `max_bytes`, so this sends the request itself.

A request's replies come on a wildcard inbox (`<inbox>.*`, the request sent with reply `<inbox>.r`),
never on a literal one: a push consumer binds only to a subject some subscription names literally
(ns:server/sublist.go:169-195), so a Node program cannot point a push consumer at Central's pull and
stream past its byte budget across the leaf. The hub refuses Central a literal inbox
(contracts.node_link.CENTRAL_SUBSCRIPTIONS; erratum E-W1-LEAF-1).

A pull tells a live consumer from a lost one (E3b design §7.2, X9, X10): any reply proves it alive, and
silence, `503` or `409 Consumer Deleted` is `ConsumerLost`, never an empty stream. `CursorReader` keeps
its own cursor (epoch, sequence) and treats the consumer as disposable (§9.4): Central's drains and a
Node's watches both read through it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, NamedTuple
from weakref import WeakKeyDictionary

import nats.errors
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, Header
from nats.js.errors import NotFoundError

from contracts.node_link import MAX_STORED_MESSAGE, NODE_MAX_CONTROL_LINE, NODE_MAX_PENDING
from nodeapi.epoch import Token, epoch_of, epoch_start

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.aio.msg import Msg
    from nats.js.api import StreamInfo

PULL_MAX_BYTES: Final = NODE_MAX_PENDING // 2
# The most one delivery charges a request's max_bytes: its subject, ack reply, headers and payload
# (ns:server/consumer.go:5516). The stored part is at most MAX_STORED_MESSAGE, the subject at most the
# control line, and 1 KiB covers the ack reply (domain, stream, consumer and five counters). Every
# request asks at least this much, so the server never ends one unfilled for a message too large.
PULL_ONE_BYTES: Final = MAX_STORED_MESSAGE + NODE_MAX_CONTROL_LINE + 1024
_GRACE_SECONDS: Final = 1.0   # past the request's expiry, for the server's closing status


class ConsumerLost(Exception):
    """The consumer answered nothing through a request's deadline plus the grace, or said it is gone
    (`503`, `409 Consumer Deleted`): a pull on a consumer that no longer exists gets no reply at all,
    which must never pass for an empty stream (X9, X10)."""


class StreamAbsent(Exception):
    """The consumer create found no such stream."""


class Pulled(NamedTuple):
    messages: list[Msg]
    complete: bool   # False: a request ended with neither a full batch nor its closing status


_COMPLETE, _LOST, _SILENT = "complete", "lost", "silent"


async def pull(client: Client, stream: str, consumer: str, batch: int, *, timeout: float,
               domain: str | None = None) -> Pulled:
    """Up to `batch` messages from the pull consumer; with every other pull on `client`, never more
    than PULL_MAX_BYTES requested and unread at once. Returns what is available at once; when nothing
    is, waits up to `timeout` for one message and adds what is available after it.

    Any reply proves the consumer alive (a message, `100`, `404`, `408`, a `409` other than Consumer
    Deleted), so `Pulled([], True)` is a live, empty consumer. Raises ConsumerLost when no request of
    the call delivered a message and one met silence, `503` or `409 Consumer Deleted`; after a
    message, the same ending returns what came with `complete` False. No request asks for idle
    heartbeats: each is short and its closing status already answers (X10).

    A request waits first for its share of the client's budget, so a pull queued behind others on one
    client can take longer than `timeout`; the one-message wait holds only PULL_ONE_BYTES, so about
    five can wait at once. No request outlives the call. Each message acknowledges as a fetched one
    does (`ack`, `ack_sync`, `metadata`)."""
    if batch < 1 or timeout <= 0:
        raise ValueError("pull_request")
    subject = f"{f'$JS.{domain}.API' if domain else '$JS.API'}.CONSUMER.MSG.NEXT.{stream}.{consumer}"
    available = {"batch": batch, "no_wait": True}
    messages, ending = await _request(client, subject, available, PULL_MAX_BYTES, timeout)
    if messages or ending != _COMPLETE:
        return _settled(messages, ending)
    messages, ending = await _request(client, subject, {"batch": 1, "expires": int(timeout * 1_000_000_000)},
                                      PULL_ONE_BYTES, timeout)
    if not messages or batch == 1 or ending != _COMPLETE:
        return _settled(messages, ending)
    rest, ending = await _request(client, subject, {**available, "batch": batch - 1}, PULL_MAX_BYTES, timeout)
    return Pulled(messages + rest, ending == _COMPLETE)


def _settled(messages: list[Msg], ending: str) -> Pulled:
    if not messages and ending != _COMPLETE:
        raise ConsumerLost(ending)
    return Pulled(messages, ending == _COMPLETE)

class _Budget:
    """The bytes a client's open pull requests may still ask for: PULL_MAX_BYTES in all."""

    def __init__(self) -> None:
        self.free = PULL_MAX_BYTES
        self.released = asyncio.Event()

    async def take(self, most: int) -> int:
        """At least PULL_ONE_BYTES and at most `most`, waiting until that much is free."""
        while self.free < PULL_ONE_BYTES:
            self.released.clear()
            await self.released.wait()
        granted = min(most, self.free)
        self.free -= granted
        return granted

    def give(self, granted: int) -> None:
        self.free += granted
        self.released.set()


_budgets: WeakKeyDictionary[Client, _Budget] = WeakKeyDictionary()


async def _request(client: Client, subject: str, request: dict, most: int,
                   timeout: float) -> tuple[list[Msg], str]:
    """One pull request, its max_bytes taken from the client's budget until it ends."""
    budget = _budgets.setdefault(client, _Budget())
    granted = await budget.take(most)
    try:
        return await _send(client, subject, {**request, "max_bytes": granted}, timeout)
    finally:
        budget.give(granted)


async def _send(client: Client, subject: str, request: dict, timeout: float) -> tuple[list[Msg], str]:
    """The request's messages and how it ended: complete (a full batch or the server's closing
    status), lost (503, 409 Consumer Deleted) or silent (nothing through the deadline plus the grace,
    which each reply extends so a slow link is not taken for silence)."""
    inbox = client.new_inbox()
    subscription = await client.subscribe(inbox + ".*")
    messages: list[Msg] = []
    ending = _SILENT
    try:
        await client.publish(subject, json.dumps(request).encode(), reply=inbox + ".r")
        deadline = time.monotonic() + timeout + _GRACE_SECONDS
        while True:
            try:   # what is already buffered is read even past the deadline (a busy loop)
                message = await subscription.next_msg(timeout=max(deadline - time.monotonic(), .001))
            except nats.errors.TimeoutError:
                break
            deadline = max(deadline, time.monotonic() + _GRACE_SECONDS)
            status = message.headers.get(Header.STATUS) if message.headers else None
            if status is None:
                messages.append(message)
                if len(messages) == request["batch"]:
                    ending = _COMPLETE
                    break
            elif status == "100":
                continue
            else:
                description = (message.headers.get(Header.DESCRIPTION) or "").lower()
                gone = status == "503" or (status == "409" and "consumer deleted" in description)
                ending = _LOST if gone else _COMPLETE   # 404 none left, 408 expired, 409 max bytes
                break
    finally:
        await subscription.unsubscribe()
    return messages, ending


class StartAt(Enum):
    FIRST = "first"                        # a drain: a new epoch from epoch_start(epoch); reports gaps
    LAST_PER_SUBJECT = "last_per_subject"  # a watch: a new epoch from each subject's latest; never reports gaps


@dataclass(frozen=True)
class Read:
    token: Token                      # (epoch, stream sequence)
    subject: str
    headers: Mapping[str, str]
    data: bytes


@dataclass(frozen=True)
class Gap:
    epoch: str
    after: int                        # the last sequence read before it
    count: int | None                 # None: unknown, the epoch ended


@dataclass(frozen=True)
class Batch:
    items: tuple[Read | Gap, ...]
    cursor: Token | None              # where the reader stands after `items`: commit it with them
    recreated: bool                   # the reader replaced its consumer while producing this batch


# Not load-bearing: a reaped consumer is noticed by its silence and recreated (E3b design §9.4).
INACTIVE_THRESHOLD_SECONDS: Final = 60.0
_DELETE_SECONDS: Final = 2.0


class CursorReader:
    """A reader that keeps its own cursor (epoch, sequence) and treats the server's consumer as
    disposable (E3b design §4 rule 1, §8, §9.4): an ephemeral AckNone consumer started at the cursor.

    Every delivery's consumer sequence must follow the last one. A jump (deliveries lost in transit
    on a leaf drop, X13), an incomplete request or a lost consumer keeps what came contiguously,
    deletes the consumer best-effort and recreates it from the cursor at the next read; the batch says
    `recreated`. A stream-sequence jump with a contiguous consumer sequence is what the stream itself
    dropped: a FIRST reader reports it as a Gap. A new epoch ends the old one: a FIRST reader reports
    Gap(old epoch, its last sequence, None) and starts at epoch_start; a LAST_PER_SUBJECT watch starts
    at each subject's latest, its cursor None until it reads."""

    def __init__(self, client: Client, stream: str, cursor: Token | None, *,
                 start: StartAt = StartAt.FIRST, domain: str | None = None) -> None:
        self._client = client
        self._jetstream = client.jetstream(domain=domain)
        self._stream = stream
        self._cursor = cursor
        self._start = start
        self._domain = domain
        self._consumer: str | None = None
        self._epoch = ""
        self._delivered = 0
        self._pending: list[Read | Gap] = []   # found at a create, not yet returned in a batch

    @property
    def cursor(self) -> Token | None:
        return self._cursor

    async def read(self, batch: int, *, timeout: float) -> Batch:
        """The next items from the cursor. Never raises ConsumerLost: it recreates its consumer.
        Raises StreamAbsent; connection errors propagate with the cursor unmoved."""
        if self._consumer is None:
            await self._create()
        try:
            pulled = await pull(self._client, self._stream, self._consumer, batch, timeout=timeout,
                                domain=self._domain)
        except ConsumerLost:
            pulled = Pulled([], False)
        intact = pulled.complete
        for message in pulled.messages:
            sequence = message.metadata.sequence
            if sequence.consumer != self._delivered + 1:
                intact = False
                break
            self._delivered = sequence.consumer
            self._take(message, sequence.stream)
        if not intact:
            await self._drop()
        items, self._pending = tuple(self._pending), []
        return Batch(items, self._cursor, not intact)

    async def close(self) -> None:
        """Delete the consumer, best-effort."""
        if self._consumer is not None:
            await self._drop()

    def _take(self, message: Msg, sequence: int) -> None:
        cursor = self._cursor
        if cursor is not None and cursor.epoch == self._epoch:
            if sequence <= cursor.seq:
                return
            if self._start is StartAt.FIRST and sequence > cursor.seq + 1:
                self._pending.append(Gap(self._epoch, cursor.seq, sequence - cursor.seq - 1))
        self._cursor = Token(self._epoch, sequence)
        self._pending.append(Read(self._cursor, message.subject, MappingProxyType(dict(message.headers or {})),
                                  message.data))

    async def _create(self) -> None:
        """A consumer at the cursor, in the stream's current epoch; the cursor and any epoch-end gap
        change only once one exists."""
        while True:
            info = await self._info()
            epoch = epoch_of(info)
            cursor, ended = self._cursor, None
            if cursor is not None and cursor.epoch != epoch:
                if self._start is StartAt.FIRST:
                    ended = Gap(cursor.epoch, cursor.seq, None)
                cursor = None
            if cursor is None and self._start is StartAt.FIRST:
                cursor = epoch_start(epoch)
            name = uuid.uuid4().hex
            if cursor is None:   # the server wants a filter for last-per-subject: the stream's own, or
                # every subject for a mirror, which has none of its own (WALL's mirror)
                position = {"deliver_policy": DeliverPolicy.LAST_PER_SUBJECT,
                            "filter_subjects": info.config.subjects or [">"]}
            else:
                position = {"deliver_policy": DeliverPolicy.BY_START_SEQUENCE, "opt_start_seq": cursor.seq + 1}
            try:
                await self._jetstream.add_consumer(self._stream, ConsumerConfig(
                    name=name, ack_policy=AckPolicy.NONE, inactive_threshold=INACTIVE_THRESHOLD_SECONDS,
                    **position))
            except NotFoundError:
                raise StreamAbsent(self._stream) from None
            if epoch_of(await self._info()) == epoch:
                break
            await self._delete(name)   # re-created under the create: start over in its new epoch
        self._consumer, self._epoch, self._delivered, self._cursor = name, epoch, 0, cursor
        if ended is not None:
            self._pending.append(ended)

    async def _info(self) -> StreamInfo:
        try:
            return await self._jetstream.stream_info(self._stream)
        except NotFoundError:
            raise StreamAbsent(self._stream) from None

    async def _drop(self) -> None:
        name, self._consumer = self._consumer, None
        if name is not None:
            await self._delete(name)

    async def _delete(self, name: str) -> None:
        # Best-effort, so leaked consumers stay off the 12-per-stream cap; one left behind is reaped
        # at its inactive threshold.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._jetstream.delete_consumer(self._stream, name), _DELETE_SECONDS)
