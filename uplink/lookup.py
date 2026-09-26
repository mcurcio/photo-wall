"""Name lookup with a timeout. getaddrinfo has none of its own: glibc waits 5 s per nameserver
attempt, so an unbounded lookup can eat a whole boot budget."""

import errno
import ipaddress
import socket
import threading


class LookupTimeout(OSError):
    """Name lookup did not finish in time."""


def _addresses(host: str, port: int, *, flags: int = 0) -> list[tuple[int, str]]:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=flags)
    except UnicodeError as error:
        # A name the idna codec refuses (an empty or 64-character label) resolves to nothing.
        raise socket.gaierror(socket.EAI_NONAME, "name cannot be encoded") from error
    return [(family, sockaddr[0]) for family, _, _, _, sockaddr in results]


def lookup(host: str, port: int, timeout: float) -> list[tuple[int, str]]:
    """(family, address) pairs in getaddrinfo order. getaddrinfo has no timeout of its own, so
    it runs on a daemon thread joined with `timeout`; LookupTimeout on expiry. IP literals
    return at once. Shared by the transport and the pool time tier.

    A timed-out thread is abandoned: harmless in a process that reboots or retries."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _addresses(host, port, flags=socket.AI_NUMERICHOST)
    outcome: dict[str, object] = {}

    def resolve() -> None:
        try:
            outcome["addresses"] = _addresses(host, port)
        except Exception as error:  # re-raised in the caller's thread below
            outcome["error"] = error

    thread = threading.Thread(target=resolve, name="uplink-lookup", daemon=True)
    thread.start()
    thread.join(max(timeout, 0.0))
    if thread.is_alive():
        raise LookupTimeout(errno.ETIMEDOUT, f"lookup of {host} took over {timeout:.1f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["addresses"]
