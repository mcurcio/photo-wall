"""The systemd notify protocol (sd_notify(3)): a single UDP datagram of newline-joined
"KEY=VALUE" assignments sent to $NOTIFY_SOCKET. Not under systemd, or the socket is gone,
is not an error here — every function degrades to a no-op `False` so callers never need to
special-case a bare shell or a stale supervisor."""

import os
import socket
from typing import Final

_ABSTRACT_PREFIX: Final = "@"


def notify(*assignments: str) -> bool:
    """Send `assignments` as one "\\n"-joined datagram to $NOTIFY_SOCKET. A leading "@" in the
    path names a Linux abstract socket, addressed with a leading NUL instead. Returns False
    when NOTIFY_SOCKET is unset or the send fails for any reason; never raises."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith(_ABSTRACT_PREFIX):
        address = "\0" + address[1:]
    payload = "\n".join(assignments).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, address)
    except OSError:
        return False
    return True


def ready() -> bool:
    """Tell systemd this service has finished starting (Type=notify)."""
    return notify("READY=1")


def pet() -> bool:
    """Reset the watchdog timer for one more WatchdogSec interval."""
    return notify("WATCHDOG=1")


def extend_start(seconds: float) -> bool:
    """Ask systemd for more start-up time, in whole microseconds."""
    return notify(f"EXTEND_TIMEOUT_USEC={int(seconds * 1_000_000)}")


def status(text: str) -> bool:
    """Publish a one-line human-readable status (newlines flattened to spaces)."""
    return notify(f"STATUS={text.replace(chr(10), ' ')}")
