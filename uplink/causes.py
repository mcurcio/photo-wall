"""Named causes (R9): every failure to reach Central is exactly one Cause plus a fixed reason
token, decided from exception types, OpenSSL verify codes and errno values, never message
text."""

import errno
import http.client
import re
import socket
import ssl
from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from uplink.lookup import LookupTimeout


class Cause(StrEnum):
    """The fixed failure vocabulary for every Central request in every stage.
    Additive only: members may be added, never renamed or removed."""

    # photowall.central invalid, unreadable or absent where the stage has no discovery;
    # broken boot data
    CONFIGURATION = "configuration"
    DNS = "dns"                  # name lookup failed or timed out
    CONNECT = "connect"          # no usable HTTP exchange up to and including the status line
    TLS = "tls"                  # untrusted chain, name mismatch, handshake, unusable trust store
    TIME = "time"                # certificate date check failed (OpenSSL verify code 9 or 10)
    REDIRECT = "redirect"        # a redirect the policy refuses
    NOT_CENTRAL = "not_central"  # locate ended on a non-redirect without Central's identity
    HTTP = "http"                # a non-200 not written by Central (gateway, proxy)
    CENTRAL = "central"          # a non-200 carrying Central's own {"error": <code>}
    TRANSFER = "transfer"        # body over its bound, short, misencoded, broken or too late


REASONS: Mapping[Cause, frozenset[str]] = MappingProxyType({
    Cause.CONFIGURATION: frozenset({"absent", "invalid", "unreadable", "floor"}),
    Cause.DNS: frozenset({"failed", "timeout"}),
    Cause.CONNECT: frozenset({"refused", "unreachable", "reset", "timeout", "closed",
                              "protocol", "other"}),
    Cause.TLS: frozenset({"untrusted", "hostname", "handshake", "trust_store"}),
    Cause.TIME: frozenset({"not_yet_valid", "expired"}),
    Cause.REDIRECT: frozenset({"downgrade", "loop", "limit", "path_changed",
                               "bad_location", "bad_status", "unexpected"}),
    Cause.NOT_CENTRAL: frozenset({"status", "body"}),
    Cause.HTTP: frozenset({"status"}),
    Cause.CENTRAL: frozenset({"error"}),
    Cause.TRANSFER: frozenset({"limit", "short", "encoding", "deadline", "tls"}),
})

CONSOLE_LIMIT = 512
_CENTRAL_ERROR = re.compile(r"[a-z0-9_]{1,64}")
_OPENSSL_REASON = re.compile(r"[A-Z0-9_]{1,64}")
# host and detail are single console tokens: printable ASCII, no space.
_TOKEN = re.compile(r"[\x21-\x7e]*")


class UplinkError(Exception):
    """One failure with exactly one cause and one reason from REASONS[cause]; any other pair
    is a TypeError at construction. `host` is printed only from a validated URL. `detail` is
    built from types, verify codes, errno names, OpenSSL reason tokens ([A-Z0-9_]{1,64}),
    statuses and validated values, never from response text. `central_error` is Central's own
    error code, kept only if it matches [a-z0-9_]{1,64}."""

    cause: Cause
    reason: str
    host: str | None
    detail: str
    central_error: str | None

    def __init__(self, cause: Cause, reason: str, *, host: str | None = None,
                 detail: str = "", central_error: str | None = None) -> None:
        if not isinstance(cause, Cause) or reason not in REASONS[cause]:
            raise TypeError(f"{reason!r} is not a reason of cause {cause!r}")
        if not _TOKEN.fullmatch(host or "") or not _TOKEN.fullmatch(detail):
            raise TypeError("host and detail must be single printable ASCII tokens")
        self.cause, self.reason, self.host, self.detail = cause, reason, host, detail
        self.central_error = (central_error if isinstance(central_error, str)
                              and _CENTRAL_ERROR.fullmatch(central_error) else None)
        super().__init__(self.console())

    def console(self) -> str:
        """'cause=<cause> reason=<reason> host=<host> detail=<detail>', at most 512 characters.
        An absent host or empty detail is left out."""
        fields = [f"cause={self.cause}", f"reason={self.reason}"]
        if self.host:
            fields.append(f"host={self.host}")
        if self.detail:
            fields.append(f"detail={self.detail}")
        return " ".join(fields)[:CONSOLE_LIMIT]


Phase = Literal["connect", "transfer"]

_CHAIN_LINKS = 8
_UNREACHABLE = frozenset({errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
                          errno.EHOSTDOWN, errno.EADDRNOTAVAIL})
_EAI_NAMES = {getattr(socket, name): name for name in dir(socket) if name.startswith("EAI_")}


def _verify_code(error: BaseException) -> int | None:
    if isinstance(error, ssl.SSLCertVerificationError):
        return getattr(error, "verify_code", None)
    return None


def _of(*types: type[BaseException]) -> Callable[[BaseException], bool]:
    return lambda error: isinstance(error, types)


# design §2.1 rules 1-16 in order; the first rule that matches any link of the chain wins.
# Rule 17 (anything else) is classify returning None.
_RULES: tuple[tuple[Phase, Callable[[BaseException], bool], Cause, str], ...] = (
    ("connect", lambda error: _verify_code(error) == 9, Cause.TIME, "not_yet_valid"),
    ("connect", lambda error: _verify_code(error) == 10, Cause.TIME, "expired"),
    ("connect", lambda error: _verify_code(error) in (62, 64), Cause.TLS, "hostname"),
    ("connect", _of(ssl.SSLCertVerificationError), Cause.TLS, "untrusted"),
    ("connect", _of(ssl.SSLError), Cause.TLS, "handshake"),
    ("connect", _of(LookupTimeout), Cause.DNS, "timeout"),
    ("connect", _of(socket.gaierror), Cause.DNS, "failed"),
    # RemoteDisconnected subclasses ConnectionResetError, so it is tested first.
    ("connect", _of(http.client.RemoteDisconnected), Cause.CONNECT, "closed"),
    ("connect", _of(TimeoutError), Cause.CONNECT, "timeout"),
    ("connect", _of(ConnectionRefusedError), Cause.CONNECT, "refused"),
    ("connect", _of(ConnectionResetError, ConnectionAbortedError, BrokenPipeError),
     Cause.CONNECT, "reset"),
    ("connect", lambda error: isinstance(error, OSError) and error.errno in _UNREACHABLE,
     Cause.CONNECT, "unreachable"),
    # Not OSError: BadStatusLine, LineTooLong, too many headers. The peer is not speaking HTTP.
    ("connect", _of(http.client.HTTPException), Cause.CONNECT, "protocol"),
    ("connect", _of(OSError), Cause.CONNECT, "other"),
    ("transfer", _of(TimeoutError), Cause.TRANSFER, "deadline"),
    ("transfer", _of(ssl.SSLError), Cause.TRANSFER, "tls"),
    ("transfer", _of(http.client.HTTPException, OSError), Cause.TRANSFER, "short"),
)


def _chain(error: BaseException) -> Iterator[BaseException]:
    """`error` and what it was raised from or during: at most 8 links, each once."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending and len(seen) < _CHAIN_LINKS:
        link = pending.pop(0)
        if link is None or any(link is other for other in seen):
            continue
        seen.append(link)
        yield link
        pending += [link.__cause__, link.__context__]


def _detail(error: BaseException) -> str:
    """A console token from the error's type, verify code, OpenSSL reason or errno name."""
    code = _verify_code(error)
    if code is not None:
        return f"verify_code={code}"
    if isinstance(error, ssl.SSLError):  # its errno is an SSL_ERROR_* value, not an errno
        reason = getattr(error, "reason", None)
        if isinstance(reason, str) and _OPENSSL_REASON.fullmatch(reason):
            return reason
    elif isinstance(error, socket.gaierror):  # its errno is an EAI_* value
        if error.errno in _EAI_NAMES:
            return _EAI_NAMES[error.errno]
    elif isinstance(error, OSError) and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return type(error).__name__


def classify(error: BaseException, *, phase: Phase, host: str | None) -> UplinkError | None:
    """Map a raw error to an UplinkError. TOTAL over network errors: every OSError (ssl.SSLError
    included) and every http.client.HTTPException maps to a cause in both phases. None is
    returned only for other types (the caller re-raises them unchanged: a programming error is
    never a network cause). Rules are tried in the table's order; each rule is checked against
    `error`, `__cause__` and `__context__` (at most 8 links, cycle-safe), so an httpx error
    wrapping an ssl error (Project 2) classifies the same way. `phase` is "connect" for
    anything raised up to and including reading the status line, else "transfer"."""
    links = list(_chain(error))
    for rule_phase, matches, cause, reason in _RULES:
        if rule_phase != phase:
            continue
        for link in links:
            if matches(link):
                return UplinkError(cause, reason, host=host, detail=_detail(link))
    return None
