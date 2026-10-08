"""Documents: one writer's conditional put and a read, on a self-describing sticky stream (E3b design
§7.2 documents, §9.2; errata E-W1-TD-4, E-W1-TD-5).

A desired bucket and WALL carry their key table in their metadata (`buffers`), so a `DocumentWriter`
learns it with the epoch from one description read at `bind`, and never writes past it: it refuses
its own unlisted, oversize or over-headered write before anything leaves the client, and the sticky
stream never evicts a document. The Node refuses nothing.

Every put is conditional (the server's expected last subject sequence): on the key's token as last
read or written, or on `ABSENT` (no value yet). A put that finds the key moved on raises `Conflict`
and is never retried here: the caller re-reads (`read`, which also refreshes the epoch and table) and
decides by the value's writer, carried in the envelope's writer header. A token names a message only
inside one creation of its stream; one from another epoch than the writer's raises `Conflict` before
sending, and on a Node stream a later creation starts at another sequence (`epoch`), so even a token
the writer never saw re-created misses. No writer deletes, purges or lists documents: an absent key
means "unknown" and its reader keeps last good.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, NamedTuple

from nats.js.api import Header
from nats.js.errors import APIError, NotFoundError

from nodeapi.buffers import HEADER_ALLOWANCE, MAX_PUBLISH_SUBJECT, KeyTable, Role, role_of, table_of
from nodeapi.envelope import caller_headers, writer_of
from nodeapi.epoch import Token, epoch_of

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.api import StreamInfo
    from nats.js.client import JetStreamContext

WRONG_LAST_SEQUENCE: Final = 10071   # a conditional write whose subject moved on since `expect`
_EXPECTED: Final = Header.EXPECTED_LAST_SUBJECT_SEQUENCE.value
_LONGEST_SEQUENCE: Final = "9" * 20   # the widest sequence a header carries


class Absent:
    """The expectation that a key holds no value yet."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final[Absent] = Absent()


class Document(NamedTuple):
    value: bytes
    writer: str | None                  # the writer header of the value; None when it has none
    token: Token


class DocumentRefused(ValueError):
    """The writer refuses its own write before sending: unlisted, too large, its headers or subject too large."""


class Conflict(Exception):
    """The key moved on since `expect` (the server's 10071), or `expect` is from another epoch."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def header_bytes(headers: Mapping[str, str] | None) -> int:
    """The header block nats-py sends for `headers` (nats/aio/client.py:1010-1026); 0 for none."""
    if not headers:
        return 0
    return len(b"NATS/1.0\r\n") + sum(len(key.encode()) + len(value.encode()) + 4
                                      for key, value in headers.items()) + 2


class DocumentWriter:
    """One writer of one desired bucket or WALL, bound to the stream's own key table and epoch."""

    def __init__(self, jetstream: JetStreamContext, stream: str, stored_prefix: str, publish_prefix: str,
                 headers: Mapping[str, str], info: StreamInfo) -> None:
        self._jetstream = jetstream
        self._stream = stream
        self._stored = stored_prefix
        self._publish = publish_prefix
        self._headers = dict(headers)
        self._epoch, self._table = self._described(info)

    @classmethod
    async def bind(cls, client: Client, stream: str, *, writer: str, domain: str | None = None) -> DocumentWriter:
        """A writer of `stream` as `writer`, from one read of its description (no options). Central
        passes domain=NODE_DOMAIN and publishes on `$JS.node.API.$KV.<bucket>.<key>`, since a plain
        `$KV.` publish never reaches the Node (W9). ValueError("document_stream") for a stream that is
        not a desired bucket or WALL with its table."""
        headers = caller_headers(writer)
        jetstream = client.jetstream(domain=domain)
        info = await jetstream.stream_info(stream)
        if role_of(info.config) not in (Role.DESIRED, Role.WALL) or not info.config.subjects:
            raise ValueError("document_stream")
        stored = info.config.subjects[0].removesuffix(">")
        publish = f"$JS.{domain}.API.{stored}" if domain else stored
        return cls(jetstream, stream, stored, publish, headers, info)

    @property
    def table(self) -> KeyTable:
        return self._table

    @property
    def epoch(self) -> str:
        return self._epoch

    def admit(self, key: str, value: bytes) -> None:
        """Raise DocumentRefused unless the stream's table admits this write."""
        if key not in self._table.sizes:
            raise DocumentRefused("document_unlisted")
        if len(value) > self._table.sizes[key]:
            raise DocumentRefused("document_too_large")
        if header_bytes({**self._headers, _EXPECTED: _LONGEST_SEQUENCE}) > HEADER_ALLOWANCE:
            raise DocumentRefused("document_headers_too_large")
        # The stored subject fits the table's check; Central's publish prefix is longer (E-W1-TD-6).
        if len((self._publish + key).encode()) > MAX_PUBLISH_SUBJECT:
            raise DocumentRefused("document_subject_past_the_control_line")

    async def put(self, key: str, value: bytes, *, expect: Token | Absent) -> Token:
        """Write one document on condition that the key is still at `expect`; its new token. Raises
        DocumentRefused before sending and Conflict when the key moved on; never retries."""
        self.admit(key, value)
        if isinstance(expect, Absent):
            last = 0
        elif expect.epoch != self._epoch:
            raise Conflict(key)
        else:
            last = expect.seq
        try:
            acknowledgement = await self._jetstream.publish(
                self._publish + key, value, headers={**self._headers, _EXPECTED: str(last)})
        except APIError as error:
            if error.err_code == WRONG_LAST_SEQUENCE:
                raise Conflict(key) from None
            raise
        return Token(self._epoch, acknowledgement.seq)

    async def read(self, key: str) -> Document | None:
        """The key's latest value, its writer and its token, from one creation of the stream; None
        when absent. Re-reads the description, so the writer's epoch and table follow the stream."""
        while True:
            info = await self._jetstream.stream_info(self._stream)
            epoch = epoch_of(info)
            try:
                message = await self._jetstream.get_last_msg(self._stream, self._stored + key)
            except NotFoundError:
                found = None
            else:
                found = Document(message.data, writer_of(message.headers), Token(epoch, message.seq))
            if epoch_of(again := await self._jetstream.stream_info(self._stream)) == epoch:
                self._epoch, self._table = self._described(again)
                return found

    @staticmethod
    def _described(info: StreamInfo) -> tuple[str, KeyTable]:
        table = table_of(info.config)
        if table is None:
            raise ValueError("document_stream")
        return epoch_of(info), table
