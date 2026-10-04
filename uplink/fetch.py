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
_RETRY_AFTER = re.compile(r"[0-9]{1,9}")


def central_error_code(body: bytes) -> str | None:
    """Central's own {"error": <[a-z0-9_]{1,64}>} from at most MAX_ERROR_BODY bytes, else None
    (a longer body, one that is not a strict JSON object, or a code of another shape)."""
    document = loads_object(body, max_bytes=MAX_ERROR_BODY)
    code = None if document is None else document.get("error")
    return code if isinstance(code, str) and CENTRAL_ERROR_CODE.fullmatch(code) else None


def retry_after_seconds(value: str | None) -> int | None:
    """A Retry-After's delta-seconds: one to nine ASCII digits, surrounding space allowed. None
    when absent, an HTTP-date, or anything else (never raises)."""
    if value is None:
        return None
    value = value.strip()
    return int(value) if _RETRY_AFTER.fullmatch(value) else None


class Refused(UplinkError):
    """A non-200 answer to a direct request: `refusal`'s cause and reason, plus the HTTP
    `status` and the answer's Retry-After in seconds (`retry_after`, None when it sent none or
    one that is not delta-seconds). `central_error` is Central's own code, else None."""

    status: int
    retry_after: int | None

    def __init__(self, cause: Cause, reason: str, *, status: int,
                 retry_after: int | None = None, **fields: str | None) -> None:
        super().__init__(cause, reason, **fields)  # type: ignore[arg-type]
        self.status, self.retry_after = status, retry_after


def refusal(url: Url, status: int, *, location: str | None, body: bytes,
            retry_after: str | None = None) -> Refused:
    """The one mapping of a non-200 answer to a direct request (DirectFetch, the Player's httpx
    requests and its websocket handshake):
      3xx               -> REDIRECT/unexpected, detail status and the Location host if it parses
      Central's body    -> CENTRAL/error, central_error=<code>
      anything else     -> HTTP/status
    `body` is what the caller read: at most MAX_ERROR_BODY + 1 bytes (b"" when it read none).
    `retry_after` is the answer's Retry-After header, read by `retry_after_seconds`."""
    host = url.origin.host
    seconds = retry_after_seconds(retry_after)
    if 300 <= status < 400:
        detail = f"status={status}"
        target = parse_url(location or "", base=url)
        if target is not None:
            detail += f";location={target.origin.host}"
        return Refused(Cause.REDIRECT, "unexpected", status=status, retry_after=seconds,
                       host=host, detail=detail)
    code = central_error_code(body)
    if code is not None:
        return Refused(Cause.CENTRAL, "error", status=status, retry_after=seconds, host=host,
                       detail=code, central_error=code)
    return Refused(Cause.HTTP, "status", status=status, retry_after=seconds, host=host,
                   detail=f"status={status}")


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive int")
    return value


def stream(transport: Transport, url: Url, maximum: int, *, block: int, deadline: float,
           headers: Mapping[str, str] | None = None,
           on_response: Callable[[Mapping[str, str]], None] | None = None,
           monotonic: Callable[[], float] = time.monotonic) -> Iterator[bytes]:
    """One direct exchange to `url`, by the absolute `deadline` (on `monotonic`'s clock; the
    caller sets it, DirectFetch within MAX_FETCH_SECONDS), streamed in non-empty blocks of at
    most `block` bytes (the consumer passes its own bound, so producer and consumer cannot
    disagree). The status line may take min(STATUS_TIMEOUT, remaining), each read
    min(READ_TIMEOUT, remaining).
      200                            -> stream (TRANSFER limit / short / encoding / deadline / tls);
                                        a body of no bytes, or of other than its declared
                                        Content-Length, is short
      non-200                        -> `refusal` (a Refused carrying status and Retry-After):
                                        any 3xx is REDIRECT / unexpected (detail: status and
                                        Location host if it parses), Central's body is
                                        CENTRAL / error, central_error=<code>, else HTTP / status
    "Central's body" is a JSON object {"error": <[a-z0-9_]{1,64}>} of at most 1 KiB,
    read with contracts.strict_json. `on_response` sees a 200's headers before its body."""
    _positive_int(maximum, "maximum")
    _positive_int(block, "block")
    host = url.origin.host
    if monotonic() >= deadline:
        raise UplinkError(Cause.TRANSFER, "deadline", host=host)
    reply = transport.send(url, headers={**FETCH_HEADERS, **(headers or {})},
                           deadline=deadline, status_timeout=STATUS_TIMEOUT)
    try:
        status = reply.status
        if status != 200:
            raise _refusal(url, reply, deadline, monotonic)
        if on_response is not None:
            on_response(reply.headers)
        length = _length(reply.headers.get("Content-Length"), maximum, host)
        encoding = reply.headers.get("Content-Encoding", "identity")
        if encoding.strip().lower() != "identity":
            raise UplinkError(Cause.TRANSFER, "encoding", host=host)
        total = 0
        while length is None or total < length:
            amount = block if length is None else min(block, length - total)
            piece = reply.read(amount, timeout=_left(deadline, monotonic, host))
            if not piece:
                break
            total += len(piece)
            if total > maximum:
                raise UplinkError(Cause.TRANSFER, "limit", host=host)
            yield piece
        if not total or (length is not None and total != length):
            raise UplinkError(Cause.TRANSFER, "short", host=host, detail=f"bytes={total}")
    finally:
        reply.close()


def _left(deadline: float, monotonic: Callable[[], float], host: str) -> float:
    """The next read's timeout: min(READ_TIMEOUT, what is left of the deadline)."""
    left = deadline - monotonic()
    if left <= 0:
        raise UplinkError(Cause.TRANSFER, "deadline", host=host)
    return min(READ_TIMEOUT, left)


def _length(value: str | None, maximum: int, host: str) -> int | None:
    """The declared Content-Length, or None when none was sent. A malformed one, or one over
    `maximum`, is `limit` (AppFetcher's bound); a declared empty body is `short`."""
    if value is None:
        return None
    if not _LENGTH.fullmatch(value) or int(value) > maximum:
        raise UplinkError(Cause.TRANSFER, "limit", host=host)
    if not int(value):
        raise UplinkError(Cause.TRANSFER, "short", host=host, detail="bytes=0")
    return int(value)


def _refusal(url: Url, reply: Reply, deadline: float, monotonic: Callable[[], float]) -> Refused:
    """The stream's use of the shared `refusal`: a 3xx never reads the body; anything else
    reads at most MAX_ERROR_BODY + 1 bytes to look for Central's own error."""
    status = reply.status
    body = b"" if 300 <= status < 400 else _error_body(reply, deadline, monotonic)
    return refusal(url, status, location=reply.headers.get("Location"), body=body,
                   retry_after=reply.headers.get("Retry-After"))


def _error_body(reply: Reply, deadline: float, monotonic: Callable[[], float]) -> bytes:
    """At most MAX_ERROR_BODY + 1 bytes of a non-200 body, or b"" if it cannot be read in time:
    a body that cannot be read is no Central body, so the status alone names the failure."""
    body = bytearray()
    try:
        while len(body) <= MAX_ERROR_BODY:
            left = deadline - monotonic()
            if left <= 0:
                break
            piece = reply.read(MAX_ERROR_BODY + 1 - len(body), timeout=min(READ_TIMEOUT, left))
            if not piece:
                break
            body += piece
    except UplinkError:
        return b""
    return bytes(body)


class DirectFetch:
    """Requests to a located origin only (the constructor takes a LocatedCentral, never a
    string), over `stream`. It keeps AppFetcher's bounds: one deadline for the whole
    acquisition, set at construction (0 < seconds <= 300); a per-read timeout of
    min(10, remaining); an exact Content-Length bound; identity encoding; a truncation check.
    The status line may take min(STATUS_TIMEOUT, remaining): Central answers a miss only after
    its read-through wait."""

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
        """`stream` of `path` at the located origin, by this fetch's deadline. The next cycle
        locates again after a 3xx; in stage 1 that is the next boot."""
        yield from stream(self._transport, self._central.origin.url(path), maximum, block=block,
                          deadline=self._deadline, headers=headers, on_response=on_response,
                          monotonic=self._monotonic)

    def get(self, path: str, maximum: int, *,
            headers: Mapping[str, str] | None = None) -> bytes:
        return b"".join(self.chunks(path, maximum, block=maximum, headers=headers))
