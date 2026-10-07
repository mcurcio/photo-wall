"""Sticky documents: the one writer's client-side budget, a reader's missing-document check, and the
epoch-scoped tokens every cursor and conditional write carries (errata E-W1-TD-4, E-W1-TD-5).

A sticky buffer (`buffers.Documents`) never evicts a document because its only writer never sends
past the table's budget: `DocumentWriter` refuses its own unlisted, oversize or over-headered write
before anything leaves the client. The Node refuses nothing. The writer also keeps a manifest
document listing every key written, so a reader (a Node component, or Central) finds a document the
stream should hold and does not: `missing_documents`. A table may have more than one writer (Central,
and a Node component writing its default), so the manifest is only ever extended by a conditional
write on the manifest as read, retried on a lost race: no writer drops another's key (E-W1-FV-4).

A stream sequence (a KV revision, a drain cursor) is a position only inside one creation of its
stream. Every token is `Token(epoch, seq)`, the epoch read from the stream's metadata; a write or
read that carries another epoch is stale and never applied (`StaleToken`): the store was lost and
re-created, and the caller re-reads. On a Node stream the new creation also starts at a sequence
derived from its epoch, so even a raw conditional write on a stale revision misses (`buffers`).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from nats.js.api import Header
from nats.js.errors import APIError, NotFoundError

from contracts.node_link import NODE_DOMAIN
from nodeapi.buffers import (
    HEADER_ALLOWANCE,
    MANIFEST_KEY,
    MAX_PUBLISH_SUBJECT,
    Documents,
)
from nodeapi.epoch import Token, stream_epoch

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.client import JetStreamContext

_REMOVED: Final = frozenset({"DEL", "PURGE"})   # KV-Operation of a deleted or purged key
WRONG_LAST_SEQUENCE: Final = 10071   # a conditional write whose subject moved on since it was read


class DocumentRefused(ValueError):
    """The writer refuses its own write, before sending: it would break the table's budget."""


class StaleToken(Exception):
    """The token was read from another creation of the stream: re-read, then write."""


def header_bytes(headers: Mapping[str, str] | None) -> int:
    """The header block nats-py sends for `headers` (nats/aio/client.py:1010-1026); 0 for none."""
    if not headers:
        return 0
    return len(b"NATS/1.0\r\n") + sum(len(key.encode()) + len(value.encode()) + 4
                                      for key, value in headers.items()) + 2


class DocumentWriter:
    """The one writer of a sticky table: it holds the table's budget (keys x history x largest value)
    and refuses its own write past it, so the sticky buffer never evicts a document."""

    def __init__(self, jetstream: JetStreamContext, table: Documents, *, publish_prefix: str | None = None) -> None:
        self.table = table
        self._jetstream = jetstream
        self._prefix = table.subject_prefix if publish_prefix is None else publish_prefix
        # Every subject this writer may publish fits the control line both servers enforce, so the
        # server never closes the writer for one (E-W1-TD-6); Central's prefix is longer than the
        # stored one, so the table's own check does not cover it.
        if any(len((self._prefix + key).encode()) > MAX_PUBLISH_SUBJECT for key in table.all_sizes()):
            raise ValueError("document_subject_past_the_control_line")
        # (epoch, keys) this writer has seen listed: only ever a reason to skip a manifest write, never
        # the content of one. A listed key stays listed in its creation, since the manifest only grows.
        self._listed: tuple[str, frozenset[str]] | None = None

    @classmethod
    def node_bucket(cls, central_client: Client, table: Documents) -> DocumentWriter:
        """Central's writer of a Node's sticky bucket across the leaf: a plain `$KV.` publish never
        reaches the Node, so it writes `$JS.node.API.$KV.<bucket>.<key>` (W9)."""
        bucket_name = table.stream.removeprefix("KV_")
        return cls(central_client.jetstream(domain=NODE_DOMAIN), table,
                   publish_prefix=f"$JS.{NODE_DOMAIN}.API.$KV.{bucket_name}.")

    def admit(self, key: str, value: bytes, headers: Mapping[str, str] | None = None) -> None:
        """Raise DocumentRefused unless the table admits this write."""
        if key not in self.table.sizes:
            raise DocumentRefused("document_unlisted")
        if len(value) > self.table.sizes[key]:
            raise DocumentRefused("document_too_large")
        if header_bytes(headers) > HEADER_ALLOWANCE:
            raise DocumentRefused("document_headers_too_large")

    async def put(self, key: str, value: bytes, *, token: Token | None = None,
                  headers: Mapping[str, str] | None = None) -> Token:
        """Write one document, conditionally on `token` (the key's revision as last read). Returns the
        new revision's token. Raises DocumentRefused before sending, StaleToken when `token` is from
        another creation of the stream, and the server's 10071 when the key moved on since."""
        headers = dict(headers or {})
        if token is not None:
            headers[Header.EXPECTED_LAST_SUBJECT_SEQUENCE.value] = str(token.seq)
        self.admit(key, value, headers)
        epoch = await stream_epoch(self._jetstream, self.table.stream)
        if token is not None and token.epoch != epoch:
            raise StaleToken(f"{self.table.stream}: token epoch {token.epoch}, stream epoch {epoch}")
        acknowledgement = await self._jetstream.publish(self._prefix + key, value, headers=headers or None)
        await self._list(epoch, key)
        return Token(epoch, acknowledgement.seq)

    async def read(self, key: str) -> tuple[bytes | None, Token | None]:
        """The key's latest value and its token (None, None when absent), from one creation."""
        while True:
            epoch = await stream_epoch(self._jetstream, self.table.stream)
            try:
                message = await self._jetstream.get_last_msg(self.table.stream, self.table.subject_prefix + key)
            except NotFoundError:
                found: tuple[bytes | None, Token | None] = (None, None)
            else:
                found = (message.data, Token(epoch, message.seq))
            if await stream_epoch(self._jetstream, self.table.stream) == epoch:
                return found

    async def _list(self, epoch: str, key: str) -> None:
        """Add `key` to the manifest unless it is listed: a compare-and-set on the manifest's last
        sequence (0: none yet), re-read and merged on every lost race, so a concurrent writer's keys
        stay. What this writer saw listed in this creation is merged in too, so a manifest a bypass
        evicted comes back whole as far as this writer knows. Only the table's keys are kept, so the
        manifest stays inside its budgeted size."""
        seen = self._listed[1] if self._listed is not None and self._listed[0] == epoch else frozenset()
        if key in seen:
            return
        while True:
            try:
                manifest = await self._jetstream.get_last_msg(self.table.stream,
                                                              self.table.subject_prefix + MANIFEST_KEY)
            except NotFoundError:
                listed, last = frozenset(), 0
            else:
                listed, last = frozenset(json.loads(manifest.data)) & self.table.sizes.keys(), manifest.seq
            if key not in listed or not seen <= listed:
                try:
                    await self._jetstream.publish(
                        self._prefix + MANIFEST_KEY, self.table.manifest(listed | seen | {key}),
                        headers={Header.EXPECTED_LAST_SUBJECT_SEQUENCE.value: str(last)})
                except APIError as error:
                    if error.err_code != WRONG_LAST_SEQUENCE:
                        raise
                    continue   # another writer extended it first: re-read and merge
                listed |= seen | {key}
            self._listed = (epoch, listed)
            return


async def listed_documents(jetstream: JetStreamContext, stream: str, subject_prefix: str) -> frozenset[str]:
    """The keys the stream's manifest lists; none when it has no manifest."""
    try:
        manifest = await jetstream.get_last_msg(stream, subject_prefix + MANIFEST_KEY)
    except NotFoundError:
        return frozenset()
    return frozenset(json.loads(manifest.data))


async def missing_documents(jetstream: JetStreamContext, stream: str, subject_prefix: str) -> frozenset[str]:
    """The documents the manifest lists that the stream does not hold (a deleted or purged key is
    missing), plus the manifest itself when the stream holds messages but no manifest. A reader
    needs only the stream and its prefix, not the writer's table: a Node reads its WALL mirror so."""
    try:
        manifest = await jetstream.get_last_msg(stream, subject_prefix + MANIFEST_KEY)
    except NotFoundError:
        held = (await jetstream.stream_info(stream)).state.messages
        return frozenset({MANIFEST_KEY}) if held else frozenset()
    missing = set()
    for key in json.loads(manifest.data):
        try:
            message = await jetstream.get_last_msg(stream, subject_prefix + key)
        except NotFoundError:
            missing.add(key)
            continue
        if message.headers and message.headers.get("KV-Operation") in _REMOVED:
            missing.add(key)
    return frozenset(missing)
