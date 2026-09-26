"""Central's origin and URLs: the one Central-root validator (R2) and the one URL grammar, which
a redirect's Location must meet as well."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urljoin, urlsplit

from uplink.causes import Cause, UplinkError

Scheme = Literal["http", "https"]
DEFAULT_PORTS: Mapping[str, int] = MappingProxyType({"http": 80, "https": 443})
MAX_URL_LENGTH = 2048


def _clean(text: str) -> bool:
    """Within the length bound, with no backslash, whitespace or control character."""
    return (len(text) <= MAX_URL_LENGTH and "\\" not in text
            and all(character.isprintable() and not character.isspace() for character in text))


@dataclass(frozen=True, slots=True)
class Origin:
    """scheme://host[:port] in canonical form: lower-case ASCII host (IDNA A-labels; IPv6
    without brackets), port always explicit (80/443 filled in). Equality is canonical: a
    non-canonical value is a ValueError at construction."""

    scheme: Scheme
    host: str
    port: int

    def __post_init__(self) -> None:
        if (self.scheme not in DEFAULT_PORTS or not self.host or not self.host.isascii()
                or self.host != self.host.lower() or not _clean(self.host)
                or any(character in "/?#@[]" for character in self.host)
                or type(self.port) is not int or not 0 < self.port < 65536):
            raise ValueError("not a canonical origin")

    @classmethod
    def parse_root(cls, text: str) -> "Origin":
        """The ONE Central-root validator. It replaces stage 1's copy now; Project 2 retires the
        Player's. The accepted set is the glossary's Central root: an http/https URL with a
        host, an optional non-zero port and an empty or `/` path; no userinfo, query,
        fragment, backslash, whitespace or control characters; at most 2048 characters.
        Anything else raises UplinkError(Cause.CONFIGURATION, "invalid")."""
        url = parse_url(text)
        if url is None or url.target != "/":
            raise UplinkError(Cause.CONFIGURATION, "invalid")
        return url.origin

    def url(self, target: str) -> "Url":
        """`target` is an absolute path (optionally with a query) starting with '/'."""
        return Url(self, target)

    def __str__(self) -> str:
        """Canonical text, default port omitted: 'https://photo-wall.example'."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == DEFAULT_PORTS[self.scheme] else f":{self.port}"
        return f"{self.scheme}://{host}{port}"


@dataclass(frozen=True, slots=True)
class Url:
    origin: Origin
    target: str                     # "/v1/locate", "/v1/netboot/base", ...

    def __post_init__(self) -> None:
        if not self.target.startswith("/") or "#" in self.target or not _clean(self.target):
            raise ValueError("a target is an absolute path with an optional query")

    def __str__(self) -> str:
        return f"{self.origin}{self.target}"


def parse_url(text: str, *, base: Url | None = None) -> Url | None:
    """`text`, resolved against `base` when given, as a canonical Url; None unless both it and
    the result are clean (see Origin.parse_root) and the result is http or https with a host,
    a non-zero port and no userinfo or fragment. Userinfo and fragment follow today's stage-1
    validator: an empty one ('http://@host/', 'http://host/#') is not refused."""
    if not isinstance(text, str) or not _clean(text):
        return None
    resolved = text if base is None else urljoin(str(base), text)
    if not _clean(resolved):
        return None
    try:
        parts = urlsplit(resolved)
        host, port = parts.hostname, parts.port
        if host is not None and not host.isascii():
            host = host.encode("idna").decode("ascii")
    except ValueError:  # a bad port or bracket; UnicodeError is a ValueError
        return None
    if (parts.scheme not in DEFAULT_PORTS or not host or port == 0 or parts.username
            or parts.password or parts.fragment):
        return None
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        return Url(Origin(parts.scheme, host, port or DEFAULT_PORTS[parts.scheme]), target)
    except ValueError:
        return None
