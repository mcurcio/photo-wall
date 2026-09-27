"""The one redirect policy (R3, R8): pure, decided before anything is sent to the next URL."""

from collections.abc import Sequence

from uplink.causes import Cause, UplinkError
from uplink.origin import Url, parse_url

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 10              # R3: above 3; the urllib and Go default


def next_hop(current: Url, status: int, location: str | None, visited: Sequence[Url]) -> Url:
    """PURE. The next URL of a locate chain, or UplinkError(REDIRECT, <reason>).
    `visited` is every URL already requested, `current` last. Checks, in order:
      1. status not in REDIRECT_STATUSES                        -> bad_status
      2. Location missing, over 2048 chars, whitespace or control characters -> bad_location
      3. resolve against `current`; scheme not http/https, userinfo or fragment -> bad_location
      4. current is https and next is http                      -> downgrade (R8)
      5. next.target != current.target (no sub-path moves)      -> path_changed
      6. next already in visited (canonical comparison)         -> loop
      7. len(visited) > MAX_REDIRECTS (this is redirect 11)     -> limit
    The error's host is the target host once step 3 has passed, else current's host."""
    host = current.origin.host
    if status not in REDIRECT_STATUSES:
        raise UplinkError(Cause.REDIRECT, "bad_status", host=host, detail=f"status={status}")
    target = None if location is None else parse_url(location, base=current)
    if target is None:
        raise UplinkError(Cause.REDIRECT, "bad_location", host=host)
    host = target.origin.host
    if current.origin.scheme == "https" and target.origin.scheme == "http":
        raise UplinkError(Cause.REDIRECT, "downgrade", host=host)
    if target.target != current.target:
        raise UplinkError(Cause.REDIRECT, "path_changed", host=host)
    hops = "hops=" + ",".join(url.origin.host for url in visited)
    if target in visited:
        raise UplinkError(Cause.REDIRECT, "loop", host=host, detail=hops)
    if len(visited) > MAX_REDIRECTS:
        raise UplinkError(Cause.REDIRECT, "limit", host=host, detail=hops)
    return target
