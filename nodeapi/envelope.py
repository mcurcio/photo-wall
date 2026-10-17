"""The envelope: the only place `nodeapi` builds or reads its headers (E3b design §7.2).

Three headers, each with a named reader: the message id on events (the server's dedupe, and
Central's projection dedupe across epochs), the schema major on events (Central's adapter per major,
C12) and the writer, on documents (C16 adoption) and on a method call (its caller). No boot id and
no clock: Central keys by epoch and sequence.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from contracts.node_link import MESSAGE_ID_HEADER, SCHEMA_MAJOR_HEADER, WRITER_HEADER


@dataclass(frozen=True)
class MethodCall:
    """One call of a component's method `<component>.method.<name>`, on a Node's bus (nodeapi.node
    NodeSession) or on the hub (nodeapi.hub `run_hub_session`): its name, its payload and its caller,
    the request's writer header ("unknown" when absent)."""
    method: str
    payload: bytes
    caller: str


MethodHandler = Callable[[MethodCall], Awaitable[bytes]]   # answers once, with the reply's payload


def event_headers(schema_major: int) -> dict[str, str]:
    """An event's headers: a fresh message id and its schema major. Built once per event, so a
    retried publish keeps its id."""
    if type(schema_major) is not int or schema_major < 0:
        raise ValueError("schema_major")
    return {MESSAGE_ID_HEADER: uuid.uuid4().hex, SCHEMA_MAJOR_HEADER: str(schema_major)}


def caller_headers(caller: str) -> dict[str, str]:
    """A method call's or a document write's headers: who sent it."""
    if type(caller) is not str or not caller:
        raise ValueError("caller")
    return {WRITER_HEADER: caller}


def message_id(headers: Mapping[str, str] | None) -> str | None:
    return (headers or {}).get(MESSAGE_ID_HEADER) or None


def schema_major(headers: Mapping[str, str] | None) -> int | None:
    value = (headers or {}).get(SCHEMA_MAJOR_HEADER)
    return int(value) if value is not None and value.isdigit() else None


def writer_of(headers: Mapping[str, str] | None) -> str | None:
    return (headers or {}).get(WRITER_HEADER) or None
