"""The shell's control socket for the bench (bead B6): it stands in for Display Host's controller
(its service stopped for the run) through appliance/display_host/weston.py `WestonBackend`, never a
second copy of the control protocol."""
from __future__ import annotations

from pathlib import Path
from typing import Final
from uuid import UUID

from contracts.display_turn import DisplayTurn

CONTROL_SOCKET: Final = Path("/run/photo-wall-display/control.sock")


class BenchController:
    """The controller's part for one Output: grant this process the screen, hand it over once a
    candidate commit was presented, turn the screen."""

    def __init__(self, output_id: str, *, socket: Path = CONTROL_SOCKET) -> None:
        """Connect and read the shell's hello. RuntimeError("bench_controller") on failure."""
        raise NotImplementedError("render-planes S1 bead B6")

    def grant(self, frame_id: str, binding_generation: int, config_revision: int, *, timeout: float) -> UUID:
        """Allow this process on the Output and send its candidate; the grant id.
        RuntimeError("bench_grant") when the shell refuses."""
        raise NotImplementedError("render-planes S1 bead B6")

    def handoff(self, *, timeout: float) -> None:
        """Release the diagnostic page to the candidate once its commit was presented.
        RuntimeError("bench_handoff") when no presented commit arrives within `timeout`."""
        raise NotImplementedError("render-planes S1 bead B6")

    def turn(self, turn: DisplayTurn, *, timeout: float) -> None:
        """Turn the screen (`WestonBackend.turn`). RuntimeError("bench_turn") when unconfirmed."""
        raise NotImplementedError("render-planes S1 bead B6")

    def close(self) -> None:
        """Withdraw the grant and disconnect."""
        raise NotImplementedError("render-planes S1 bead B6")
