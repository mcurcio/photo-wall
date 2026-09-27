"""Name lookup with a timeout. getaddrinfo has none of its own: glibc waits 5 s per nameserver
attempt, so an unbounded lookup can eat a whole boot budget."""

import errno
import ipaddress
import socket
import threading
from typing import Final

MAX_PENDING_LOOKUPS: Final = 4
_abandoned = 0                      # abandoned resolver threads still running
_abandoned_lock = threading.Lock()


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

    At most MAX_PENDING_LOOKUPS abandoned threads may run at once; past that a name lookup
    times out at once, without starting a thread."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _addresses(host, port, flags=socket.AI_NUMERICHOST)
    global _abandoned
    with _abandoned_lock:
        if _abandoned >= MAX_PENDING_LOOKUPS:
            raise LookupTimeout(errno.ETIMEDOUT, f"lookup of {host} refused: "
                                f"{MAX_PENDING_LOOKUPS} earlier lookups still pending")
    outcome: dict[str, object] = {}
    state = {"done": False, "abandoned": False}

    def resolve() -> None:
        global _abandoned
        try:
            outcome["addresses"] = _addresses(host, port)
        except Exception as error:  # re-raised in the caller's thread below
            outcome["error"] = error
        finally:
            with _abandoned_lock:
                state["done"] = True
                if state["abandoned"]:
                    _abandoned -= 1

    thread = threading.Thread(target=resolve, name="uplink-lookup", daemon=True)
    thread.start()
    thread.join(max(timeout, 0.0))
    if thread.is_alive():
        with _abandoned_lock:
            if not state["done"]:
                state["abandoned"] = True
                _abandoned += 1
                raise LookupTimeout(errno.ETIMEDOUT, f"lookup of {host} took over {timeout:.1f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["addresses"]
