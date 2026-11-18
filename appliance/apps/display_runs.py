"""The broker tells Display which Player run is current (decision 0034, S4b part 4, E3).

The app broker is the app lifecycle's one judge of which run is current; Display hands an Output
to the Player only for that run. On each known turn the broker hands its current run here, and
`DisplayRuns` sends it to Display's root-only ingress (`INGRESS_SOCKET`) as one `app_run` packet
(`appliance.kernel.app_facts.AppRunOp`) when it differs from what Display last accepted, or when
the ingress socket is a new one (a new controller, which holds no run). Otherwise a turn costs one
`stat`. The op's shape and the socket's path live in the kernel: App lifecycle may not import
Display.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Final

from appliance.apps.probe import AppRunKey
from appliance.kernel.app_facts import APP_RUN_OP, AppRunOp
from appliance.kernel.display_paths import INGRESS_SOCKET

LOG = logging.getLogger(__name__)
MAX_ANSWER: Final = 4096      # Display's answer to `app_run` is a few bytes


class DisplayRuns:
    """The broker's current run, told to Display's ingress (op APP_RUN_OP on INGRESS_SOCKET, one
    SOCK_SEQPACKET packet; the sender's credentials ride it, the listener checks uid 0)."""

    def __init__(self, path: Path = INGRESS_SOCKET, *, timeout: float = 1.0) -> None:
        self._path, self._timeout = path, timeout
        self._delivered: tuple[AppRunKey | None, tuple[int, int]] | None = None   # (run, socket) accepted
        self._failure: str | None = None   # the cause last logged, until a delivery succeeds

    def tell(self, run: AppRunKey | None) -> bool:
        """Whether Display holds `run` now. Sends only when `run`, or the ingress socket's identity
        (st_ino, st_ctime_ns: a new controller), differs from the last delivery Display accepted; a
        refusal or OSError is logged once per cause and sent again at the next call. Never raises
        OSError; never blocks past `timeout`."""
        try:
            status = os.stat(self._path)
        except OSError as error:
            return self._failed(f"no ingress ({type(error).__name__})")
        delivery = (run, (status.st_ino, status.st_ctime_ns))
        if delivery == self._delivered:
            return True
        packet: AppRunOp = {"op": APP_RUN_OP, "run": None if run is None else run.document()}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
                connection.settimeout(self._timeout)
                if hasattr(socket, "SO_PASSCRED"):   # the credentials ride every packet (Linux)
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                connection.connect(str(self._path))
                connection.sendall(json.dumps(packet, separators=(",", ":")).encode())
                answer = json.loads(connection.recv(MAX_ANSWER) or b"null")
        except (OSError, ValueError) as error:
            return self._failed(f"not told ({type(error).__name__})")
        if not isinstance(answer, dict) or answer.get("accepted") is not True:
            reason = answer.get("reason") if isinstance(answer, dict) else None
            return self._failed(f"refused ({reason})")
        self._delivered, self._failure = delivery, None
        return True

    def _failed(self, cause: str) -> bool:
        """Display does not hold the run: logged once per cause, told again at the next call."""
        self._delivered = None
        if cause != self._failure:
            self._failure = cause
            LOG.warning("broker: Display was not told the current run: %s; telling it again", cause)
        return False


__all__ = ["DisplayRuns"]
