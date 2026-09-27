"""Direct requests (R7, R8): only to the origin locate found, and never through a redirect. A 3xx
here means the gateway moved Central since locate ran; the next cycle locates again."""

import re
import time
from collections.abc import Callable, Iterator, Mapping
from types import MappingProxyType
from typing import Final

from contracts.read_through import READ_THROUGH_WAIT_SECONDS
from contracts.strict_json import loads_object
from uplink.causes import CENTRAL_ERROR_CODE, Cause, UplinkError
from uplink.locate import LocatedCentral
from uplink.origin import Url, parse_url
from uplink.transport import HOP_TIMEOUT, Reply, Transport

MAX_ERROR_BODY: Final = 1024
MAX_FETCH_SECONDS: Final = 300.0    # the acquisition deadline's ceiling (AppFetcher's bound)
READ_TIMEOUT: Final = 10.0          # per read: min(this, what is left of the deadline)
# Up to the status line: Central may hold a miss for its read-through wait, plus one hop's bound
# for everything else. Still capped by the deadline.
STATUS_TIMEOUT: Final = READ_THROUGH_WAIT_SECONDS + HOP_TIMEOUT
FETCH_HEADERS: Mapping[str, str] = MappingProxyType({"Accept-Encoding": "identity"})

_LENGTH = re.compile(r"[0-9]{1,12}")


def central_error_code(body: bytes) -> str | None:
    """Central's own {"error": <[a-z0-9_]{1,64}>} from at most MAX_ERROR_BODY bytes, else None
    (a longer body, one that is not a strict JSON object, or a code of another shape)."""
    document = loads_object(body, max_bytes=MAX_ERROR_BODY)
    code = None if document is None else document.get("error")
    return code if isinstance(code, str) and CENTRAL_ERROR_CODE.fullmatch(code) else None


def refusal(url: Url, status: int, *, location: str | None, body: bytes) -> UplinkError:
    """The one mapping of a non-200 answer to a direct request (DirectFetch, the Player's httpx
    requests and its websocket handshake):
      3xx               -> REDIRECT/unexpected, detail status and the Location host if it parses
      Central's body    -> CENTRAL/error, central_error=<code>
      anything else     -> HTTP/status
    `body` is what the caller read: at most MAX_ERROR_BODY + 1 bytes (b"" when it read none)."""
    host = url.origin.host
    if 300 <= status < 400:
        detail = f"status={status}"
        target = parse_url(location or "", base=url)
        if target is not None:
            detail += f";location={target.origin.host}"
        return UplinkError(Cause.REDIRECT, "unexpected", host=host, detail=detail)
    code = central_error_code(body)
    if code is not None:
        return UplinkError(Cause.CENTRAL, "error", host=host, detail=code, central_error=code)
    return UplinkError(Cause.HTTP, "status", host=host, detail=f"status={status}")


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive int")
    return value


class DirectFetch:
    """Requests to a located origin only (the constructor takes a LocatedCentral, never a
    string). It keeps AppFetcher's bounds: one deadline for the whole acquisition, set at
    construction (0 < seconds <= 300); a per-read timeout of min(10, remaining); an exact
    Content-Length bound; identity encoding; a truncation check. The status line may take
    min(STATUS_TIMEOUT, remaining): Central answers a miss only after its read-through wait."""

    __slots__ = ("_central", "_transport", "_deadline", "_monotonic")

    def __init__(self, central: LocatedCentral, *, transport: Transport, seconds: float,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(central, LocatedCentral):
            raise TypeError("DirectFetch goes only to a LocatedCentral, never to a string")
        if not 0 < seconds <= MAX_FETCH_SECONDS:
            raise ValueError(f"seconds must be in (0, {MAX_FETCH_SECONDS:g}]")
        self._central, self._transport, self._monotonic = central, transport, monotonic
        self._deadline = monotonic() + seconds

    def chunks(self, path: str, maximum: int, *, block: int,
               headers: Mapping[str, str] | None = None,
               on_response: Callable[[Mapping[str, str]], None] | None = None) -> Iterator[bytes]:
        """Stream the body in non-empty blocks of at most `block` bytes (the consumer passes its
        own bound, so producer and consumer cannot disagree).
          200                            -> stream (TRANSFER limit / short / encoding / deadline / tls)
          any 3xx                        -> REDIRECT / unexpected (detail: status and Location
                                            host if it parses). The next cycle locates again;
                                            in stage 1 that is the next boot
          non-200 with Central's body    -> CENTRAL / error, central_error=<code>
          non-200 without it             -> HTTP / status
        "Central's body" is a JSON object {"error": <[a-z0-9_]{1,64}>} of at most 1 KiB,
        read with contracts.strict_json. `on_response` sees a 200's headers before its body."""
        _positive_int(maximum, "maximum")
        _positive_int(block, "block")
        url = self._central.origin.url(path)
        host = url.origin.host
        if self._monotonic() >= self._deadline:
            raise UplinkError(Cause.TRANSFER, "deadline", host=host)
        reply = self._transport.send(url, headers={**FETCH_HEADERS, **(headers or {})},
                                     deadline=self._deadline, status_timeout=STATUS_TIMEOUT)
        try:
            status = reply.status
            if status != 200:
                raise self._refusal(url, reply)
            if on_response is not None:
                on_response(reply.headers)
            length = self._length(reply.headers.get("Content-Length"), maximum, host)
            encoding = reply.headers.get("Content-Encoding", "identity")
            if encoding.strip().lower() != "identity":
                raise UplinkError(Cause.TRANSFER, "encoding", host=host)
            total = 0
            while length is None or total < length:
                amount = block if length is None else min(block, length - total)
                piece = reply.read(amount, timeout=self._left(host))
                if not piece:
                    break
                total += len(piece)
                if total > maximum:
                    raise UplinkError(Cause.TRANSFER, "limit", host=host)
                yield piece
            if not total or (length is not None and total != length):
                raise UplinkError(Cause.TRANSFER, "short", host=host,
                                  detail=f"bytes={total}")
        finally:
            reply.close()

    def get(self, path: str, maximum: int, *,
            headers: Mapping[str, str] | None = None) -> bytes:
        return b"".join(self.chunks(path, maximum, block=maximum, headers=headers))

    def _left(self, host: str) -> float:
        """The next read's timeout: min(READ_TIMEOUT, what is left of the deadline)."""
        left = self._deadline - self._monotonic()
        if left <= 0:
            raise UplinkError(Cause.TRANSFER, "deadline", host=host)
        return min(READ_TIMEOUT, left)

    @staticmethod
    def _length(value: str | None, maximum: int, host: str) -> int | None:
        """The declared Content-Length, or None when none was sent. A malformed one, or one
        over `maximum`, is `limit` (AppFetcher's bound); a declared empty body is `short`."""
        if value is None:
            return None
        if not _LENGTH.fullmatch(value) or int(value) > maximum:
            raise UplinkError(Cause.TRANSFER, "limit", host=host)
        if not int(value):
            raise UplinkError(Cause.TRANSFER, "short", host=host, detail="bytes=0")
        return int(value)

    def _refusal(self, url: Url, reply: Reply) -> UplinkError:
        """DirectFetch's use of the shared `refusal`: a 3xx never reads the body (as today);
        anything else reads at most MAX_ERROR_BODY + 1 bytes to look for Central's own error."""
        status = reply.status
        body = b"" if 300 <= status < 400 else self._error_body(reply)
        return refusal(url, status, location=reply.headers.get("Location"), body=body)

    def _error_body(self, reply: Reply) -> bytes:
        """At most MAX_ERROR_BODY + 1 bytes of a non-200 body, or b"" if it cannot be read in
        time: a body that cannot be read is no Central body, so the status alone names the
        failure."""
        body = bytearray()
        try:
            while len(body) <= MAX_ERROR_BODY:
                left = self._deadline - self._monotonic()
                if left <= 0:
                    break
                piece = reply.read(MAX_ERROR_BODY + 1 - len(body),
                                   timeout=min(READ_TIMEOUT, left))
                if not piece:
                    break
                body += piece
        except UplinkError:
            return b""
        return bytes(body)
