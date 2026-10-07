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
the only kind the hub lets Central subscribe to (contracts.node_link.CENTRAL_SUBSCRIPTIONS). Across
the leaf, a pull's replies come back only on the response the Node's server tracks for it, which only
JetStream answers (contracts.node_link.NODE_PULL_SERVICE; errata E-W1-LEAF-1, E-W1-LEAF-2), so what
crosses for a pull is at most the bytes it asked for. That route ends at the server's response
threshold, 2 minutes: a pull across the leaf waits less.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Final
from weakref import WeakKeyDictionary

import nats.errors
from nats.js.api import Header

from contracts.node_link import MAX_STORED_MESSAGE, NODE_MAX_CONTROL_LINE, NODE_MAX_PENDING

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.aio.msg import Msg

PULL_MAX_BYTES: Final = NODE_MAX_PENDING // 2
# The most one delivery charges a request's max_bytes: its subject, ack reply, headers and payload
# (ns:server/consumer.go:5516). The stored part is at most MAX_STORED_MESSAGE, the subject at most the
# control line, and 1 KiB covers the ack reply (domain, stream, consumer and five counters). Every
# request asks at least this much, so the server never ends one unfilled for a message too large.
PULL_ONE_BYTES: Final = MAX_STORED_MESSAGE + NODE_MAX_CONTROL_LINE + 1024
_GRACE_SECONDS: Final = 1.0   # past the request's expiry, for the server's closing status


async def pull(client: Client, stream: str, durable: str, batch: int, *, timeout: float,
               domain: str | None = None) -> list[Msg]:
    """Up to `batch` messages from the durable pull consumer; with every other pull on `client`, never
    more than PULL_MAX_BYTES requested and unread at once. Returns what is available at once; when
    nothing is, waits up to `timeout` for one message and adds what is available after it; none when
    nothing arrives. A request waits first for its share of the client's budget, so a pull queued
    behind others on one client can take longer than `timeout`; the one-message wait holds only
    PULL_ONE_BYTES, so about five can wait at once. No request outlives the call, so no message waits
    undelivered past the consumer's ack wait. Each message acknowledges as a fetched one does (`ack`,
    `ack_sync`, `metadata`)."""
    if batch < 1 or timeout <= 0:
        raise ValueError("pull_request")
    subject = f"{f'$JS.{domain}.API' if domain else '$JS.API'}.CONSUMER.MSG.NEXT.{stream}.{durable}"
    available = {"batch": batch, "no_wait": True}
    if messages := await _request(client, subject, available, PULL_MAX_BYTES, timeout):
        return messages
    first = await _request(client, subject, {"batch": 1, "expires": int(timeout * 1_000_000_000)},
                           PULL_ONE_BYTES, timeout)
    if not first or batch == 1:
        return first
    return first + await _request(client, subject, {**available, "batch": batch - 1}, PULL_MAX_BYTES, timeout)


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


async def _request(client: Client, subject: str, request: dict, most: int, timeout: float) -> list[Msg]:
    """One pull request, its max_bytes taken from the client's budget until it ends: its messages
    until the batch is full or the server's status ends it."""
    budget = _budgets.setdefault(client, _Budget())
    granted = await budget.take(most)
    try:
        return await _send(client, subject, {**request, "max_bytes": granted}, timeout)
    finally:
        budget.give(granted)


async def _send(client: Client, subject: str, request: dict, timeout: float) -> list[Msg]:
    inbox = client.new_inbox()
    subscription = await client.subscribe(inbox + ".*")
    messages: list[Msg] = []
    try:
        await client.publish(subject, json.dumps(request).encode(), reply=inbox + ".r")
        deadline = time.monotonic() + timeout + _GRACE_SECONDS
        while len(messages) < request["batch"] and (left := deadline - time.monotonic()) > 0:
            try:
                message = await subscription.next_msg(timeout=left)
            except nats.errors.TimeoutError:
                break
            status = message.headers.get(Header.STATUS) if message.headers else None
            if status is None:
                messages.append(message)
            elif status != "100":   # 404 none left, 408 expired, 409 max bytes or a closed request
                break
    finally:
        await subscription.unsubscribe()
    return messages
