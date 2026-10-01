"""Bounded JSON POST to a located Central; never follows a redirect."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from contracts.strict_json import loads_object
from uplink.causes import Cause, UplinkError
from uplink.fetch import refusal
from uplink.locate import LocatedCentral
from uplink.transport import Transport

MAX_BODY = 2048
MAX_REPLY = 4096


class UnsupportedRoute(Exception):
    """A located older Central returned its canonical unknown-route response."""


def post_json(central: LocatedCentral, path: str, value: Mapping[str, object], *,
              transport: Transport, seconds: float = 15,
              max_reply: int = MAX_REPLY) -> bytes:
    """One bounded exchange; an HTTP 404 is the caller's route-specific decision."""
    if not 0 < seconds <= 60 or not 0 < max_reply <= 16384:
        raise ValueError("invalid POST bound")
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > MAX_BODY:
        raise ValueError("POST body too large")
    url = central.origin.url(path)
    deadline = time.monotonic() + seconds
    reply = transport.send(url, headers={"Accept-Encoding": "identity",
                                         "Content-Type": "application/json"},
                           deadline=deadline, method="POST", body=body)
    try:
        if reply.status != 200:
            error = b"" if 300 <= reply.status < 400 else _read(reply, 1024, deadline, url.origin.host)
            if (reply.status == 404
                    and loads_object(error, max_bytes=1024) == {"detail": "Not Found"}):
                raise UnsupportedRoute(path)
            raise refusal(url, reply.status, location=reply.headers.get("Location"), body=error)
        if reply.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise UplinkError(Cause.TRANSFER, "encoding", host=url.origin.host)
        response = _read(reply, max_reply, deadline, url.origin.host)
        if not response:
            raise UplinkError(Cause.TRANSFER, "short", host=url.origin.host)
        return response
    finally:
        reply.close()


def _read(reply, maximum: int, deadline: float, host: str) -> bytes:
    result = bytearray()
    while len(result) <= maximum:
        left = deadline - time.monotonic()
        if left <= 0:
            raise UplinkError(Cause.TRANSFER, "deadline", host=host)
        block = reply.read(maximum + 1 - len(result), timeout=min(10, left))
        if not block:
            break
        result.extend(block)
    if len(result) > maximum:
        raise UplinkError(Cause.TRANSFER, "limit", host=host)
    return bytes(result)
