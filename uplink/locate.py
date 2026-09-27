"""Locate (R3, R7, R8): the one request that follows redirects. It must end on Central's
identity; the origin of the URL where it ends is where every direct request then goes."""

import time
from collections.abc import Callable, Mapping
from types import MappingProxyType

from contracts.central_identity import (
    LOCATE_PATH,
    MAX_IDENTITY_BYTES,
    CentralIdentity,
    parse_identity,
)
from uplink.causes import Cause, UplinkError
from uplink.origin import Origin, Url
from uplink.redirects import next_hop
from uplink.transport import Reply, Transport

LOCATE_DEADLINE = 30.0          # the whole chain; two lookups + two hops need 24 s at worst
LOCATE_HEADERS: Mapping[str, str] = MappingProxyType(
    {"Accept": "application/json", "Accept-Encoding": "identity"})   # no credential, no serial

_PROOF = object()               # only locate() holds it, so only locate() builds a LocatedCentral


class LocatedCentral:
    """Built only by locate(). NOT a dataclass: __init__ requires a module-private proof object
    and raises TypeError otherwise; there is no __replace__, so dataclasses.replace and
    copy.replace both raise TypeError; __slots__ plus a refusing __setattr__ make it read-only.
    Guarantee: construction-time for every public construction path. A deliberate
    object.__new__ cannot be prevented in Python and is out of scope (convention + test).
    `origin` is the origin of the URL where locate ended."""

    __slots__ = ("_origin", "_identity", "_hops")

    def __init__(self, origin: Origin, identity: CentralIdentity, hops: tuple[Url, ...], *,
                 proof: object) -> None:
        if proof is not _PROOF:
            raise TypeError("a LocatedCentral is built only by locate()")
        object.__setattr__(self, "_origin", origin)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_hops", hops)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"LocatedCentral is read-only: cannot set {name}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"LocatedCentral is read-only: cannot delete {name}")

    def __repr__(self) -> str:
        return f"LocatedCentral(origin={self._origin}, hops={len(self._hops)})"

    @property
    def origin(self) -> Origin:
        return self._origin

    @property
    def identity(self) -> CentralIdentity:
        return self._identity

    @property
    def hops(self) -> tuple[Url, ...]:
        return self._hops


def _read_identity(reply: Reply, deadline: float, monotonic: Callable[[], float]) -> bytes:
    """At most MAX_IDENTITY_BYTES + 1: one byte over is enough to refuse the body."""
    body = bytearray()
    while len(body) <= MAX_IDENTITY_BYTES:
        chunk = reply.read(MAX_IDENTITY_BYTES + 1 - len(body), timeout=deadline - monotonic())
        if not chunk:
            break
        body += chunk
    return bytes(body)


def locate(root: Origin, *, transport: Transport,
           on_hop: Callable[[Url, int, str], None] | None = None,
           monotonic: Callable[[], float] = time.monotonic) -> LocatedCentral:
    """GET root + /v1/locate and follow redirects through next_hop, all under one deadline of
    LOCATE_DEADLINE. Stop at the first non-3xx:
      200 whose body passes parse_identity  -> LocatedCentral(origin of that URL)
      5xx without an identity               -> HTTP / status
      any other non-3xx without an identity -> NOT_CENTRAL / status (or body for a 200)
    Reads at most MAX_IDENTITY_BYTES + 1 of the final body; never reads a redirect body.
    Nothing is sent to a URL next_hop refused. A deadline that runs out is named by the wait
    it interrupted (§2.1). on_hop(url, status, peer) feeds the console. Nothing is persisted
    (choice, 0014)."""
    deadline = monotonic() + LOCATE_DEADLINE
    url = root.url(LOCATE_PATH)
    visited: list[Url] = []
    while True:
        visited.append(url)
        reply = transport.send(url, headers=LOCATE_HEADERS, deadline=deadline)
        try:
            status = reply.status
            if on_hop is not None:
                on_hop(url, status, reply.peer)
            if 300 <= status < 400:
                url = next_hop(url, status, reply.headers.get("Location"), visited)
                continue
            body = _read_identity(reply, deadline, monotonic) if status == 200 else b""
        finally:
            reply.close()
        host, detail = url.origin.host, f"status={status}"
        if status == 200:
            identity = parse_identity(body)
            if identity is None:
                raise UplinkError(Cause.NOT_CENTRAL, "body", host=host, detail=detail)
            return LocatedCentral(url.origin, identity, tuple(visited), proof=_PROOF)
        if 500 <= status < 600:
            raise UplinkError(Cause.HTTP, "status", host=host, detail=detail)
        raise UplinkError(Cause.NOT_CENTRAL, "status", host=host, detail=detail)
