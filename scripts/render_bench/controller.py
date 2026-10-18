"""The shell's control socket for the bench (bead B6): it stands in for Display Host's controller
(its service stopped for the run) through appliance/display_host/weston.py `WestonBackend`, never a
second copy of the control protocol.

The bench is both the controller and the app: the shell takes a control peer only with the
controller's uid (pw-display) and checks the app's Wayland peer against the grant's pid, start time
and uid, so __main__ makes both connections as pw-display and this process is the one granted."""
from __future__ import annotations

import os
import socket as sockets
import struct
import time
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from appliance.display_host.weston import (
    CompositorPresentation,
    DiagnosticRelease,
    SurfaceGrant,
    WestonBackend,
)
from contracts.display_turn import DisplayTurn
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity

CONTROL_SOCKET: Final = Path("/run/photo-wall-display/control.sock")
_IDLE: Final = 0.005            # seconds between dispatches while waiting on the shell


def _start_ticks() -> int:
    """This process's start time in clock ticks (/proc/self/stat field 22), as the shell checks."""
    return int(Path("/proc/self/stat").read_text().rpartition(") ")[2].split()[19])


class BenchController:
    """The controller's part for one Output: grant this process the screen, hand it over once a
    candidate commit was presented, turn the screen."""

    def __init__(self, output_id: str, *, socket: Path = CONTROL_SOCKET) -> None:
        """Connect and read the shell's hello. RuntimeError("bench_controller") on failure. The
        grant's uid is this process's effective uid now (the Wayland connection's, made as the
        same user)."""
        self._output_id = output_id
        self._uid = os.geteuid()
        self._surface: Surface | None = None
        self._authorized = False
        try:
            channel = sockets.socket(sockets.AF_UNIX, sockets.SOCK_SEQPACKET)
            channel.connect(str(socket))
            pid, uid, _gid = struct.unpack("3i", channel.getsockopt(sockets.SOL_SOCKET, sockets.SO_PEERCRED, 12))
            self._backend = WestonBackend(channel, display_uid=uid, compositor_pid=pid,
                                          verify_process=lambda grant: grant.surface.process.pid == os.getpid())
            self._host = self._backend.initialize()
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError("bench_controller") from error

    def grant(self, frame_id: str, binding_generation: int, config_revision: int, *, timeout: float) -> UUID:
        """Allow this process on the Output and send its candidate; the grant id.
        RuntimeError("bench_grant") when the shell refuses. A grant after a turn waits for the
        Output's new key (a turn is a mode change: the shell took the old grant back)."""
        end = time.monotonic() + timeout
        try:
            while (key := self._key()) is None or (self._surface is not None and key == self._surface.output):
                if time.monotonic() >= end:
                    raise RuntimeError("bench_grant")
                self._dispatch()
            surface = Surface(key, NodeProcessIdentity(os.getpid(), _start_ticks(), uuid4()), 1,
                              binding_generation, config_revision, frame_id)
            grant_id = self._backend.allow(SurfaceGrant(surface, self._uid, True))
            self._host.offer(surface)
        except (OSError, ValueError, KeyError, TimeoutError) as error:
            raise RuntimeError("bench_grant") from error
        self._surface, self._authorized = surface, False
        return grant_id

    def handoff(self, *, timeout: float) -> None:
        """Release the diagnostic page to the candidate once its commit was presented.
        RuntimeError("bench_handoff") when no presented commit arrives within `timeout`. A call
        that ran out of time keeps what it reached: the next call goes on from there (the bench
        presents between calls)."""
        if self._surface is None:
            raise RuntimeError("bench_handoff")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            observed = self._backend.dispatch()
            if isinstance(observed, CompositorPresentation) and not self._authorized:
                try:
                    self._host.authorize_handoff(self._surface, now_ms=observed.observed_monotonic_ms)
                    self._authorized = True
                except ValueError:
                    continue            # a newer presentation supersedes it
            elif isinstance(observed, DiagnosticRelease) and observed.surface == self._surface:
                return
            elif observed is None:
                time.sleep(_IDLE)
        raise RuntimeError("bench_handoff")

    def drain(self) -> None:
        """Dispatch every event the shell has sent (it drops a control peer that lags)."""
        for _ in range(4096):
            if self._backend.dispatch() is None:
                return

    def turn(self, turn: DisplayTurn, *, timeout: float) -> None:
        """Turn the screen (`WestonBackend.turn`). RuntimeError("bench_turn") when unconfirmed."""
        if not self._backend.turn(self._output_id, turn, timeout=timeout):
            raise RuntimeError("bench_turn")

    def close(self) -> None:
        """Withdraw the grant and disconnect."""
        try:
            if self._surface is not None:
                self._backend.withdraw(self._surface, uuid4())
        except (OSError, ValueError, KeyError, TimeoutError):
            pass
        finally:
            self._backend.channel.close()

    def _key(self):
        """The Output's current key; None before the shell announced it connected."""
        try:
            state = self._host.state(self._output_id)
        except KeyError:
            return None
        return state.key if state.connected else None

    def _dispatch(self) -> None:
        if self._backend.dispatch() is None:
            time.sleep(_IDLE)


__all__ = ["CONTROL_SOCKET", "BenchController"]
