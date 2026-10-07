"""The one way to pull from a JetStream consumer: every pull request carries a byte cap (E-W1-TD-3).

nats-server closes a local client whose undelivered bytes pass `max_pending` (a slow consumer,
ns:server/client.go:2624-2636), which a component whose loop is busy while a large batch arrives
reaches at once. The server queues for one pull at most the request's `max_bytes`, so every pull
asks for at most PULL_MAX_BYTES, half the Node's max_pending (contracts.node_link.NODE_MAX_PENDING,
which the config test binds to node-bus.conf): the other half is the connection's other traffic.
A busy reader then slows the server's writes, never ends its connection (`write_timeout: retry`).
nats-py 2.16.0's `fetch` sends no `max_bytes`, so this sends the request itself.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Final

import nats.errors
from nats.js.api import Header

from contracts.node_link import NODE_MAX_PENDING

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.aio.msg import Msg

PULL_MAX_BYTES: Final = NODE_MAX_PENDING // 2
_GRACE_SECONDS: Final = 1.0   # past the request's expiry, for the server's closing status


async def pull(client: Client, stream: str, durable: str, batch: int, *, timeout: float,
               domain: str | None = None) -> list[Msg]:
    """Up to `batch` messages from the durable pull consumer, never more than PULL_MAX_BYTES in one
    request. Returns what is available at once; when nothing is, waits up to `timeout` for one
    message and adds what is available after it; none when nothing arrives. No request outlives the
    call, so no message waits undelivered past the consumer's ack wait. Each message acknowledges as
    a fetched one does (`ack`, `ack_sync`, `metadata`)."""
    if batch < 1 or timeout <= 0:
        raise ValueError("pull_request")
    subject = f"{f'$JS.{domain}.API' if domain else '$JS.API'}.CONSUMER.MSG.NEXT.{stream}.{durable}"
    available = {"batch": batch, "max_bytes": PULL_MAX_BYTES, "no_wait": True}
    if messages := await _request(client, subject, available, timeout):
        return messages
    first = await _request(client, subject, {"batch": 1, "max_bytes": PULL_MAX_BYTES,
                                             "expires": int(timeout * 1_000_000_000)}, timeout)
    if not first or batch == 1:
        return first
    return first + await _request(client, subject, {**available, "batch": batch - 1}, timeout)


async def _request(client: Client, subject: str, request: dict, timeout: float) -> list[Msg]:
    """One pull request: its messages until the batch is full or the server's status ends it."""
    inbox = client.new_inbox()
    subscription = await client.subscribe(inbox)
    messages: list[Msg] = []
    try:
        await client.publish(subject, json.dumps(request).encode(), reply=inbox)
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
