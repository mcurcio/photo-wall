"""The display controller's glitch evidence (bead B6): the pixel valve's sticky status bits (bench T2;
the kernel's hvs_underrun counter is dead on the Pi 5), read from /dev/mem as root. They stay set
until the next full modeset, so ten reads a second miss none."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PV0_BASE: Final = 0x107C410000      # pixel valve 0 (HDMI-A-1), test2/src/hvsbench.c
PV_STATUS: Final = 0x2C             # its status register
STICKY_GLITCH: Final = 0xA00        # the bits an underrun sets and leaves set


@dataclass(frozen=True)
class RegisterSample:
    """One read of the pixel valve's status at monotonic `at`."""
    at: float
    pv_status: int


def glitched(first: RegisterSample, sample: RegisterSample) -> bool:
    """Whether a STICKY_GLITCH bit is set in `sample` that was clear in `first`. Pure."""
    raise NotImplementedError("render-planes S1 bead B6")


class RegisterWatch:
    """Reads the pixel valve's status through a read-only mapping of /dev/mem."""

    def __init__(self, memory: str = "/dev/mem") -> None:
        """Map PV0_BASE read-only. PermissionError when not root."""
        raise NotImplementedError("render-planes S1 bead B6")

    def sample(self) -> RegisterSample:
        """The status now."""
        raise NotImplementedError("render-planes S1 bead B6")

    def close(self) -> None:
        """Unmap."""
        raise NotImplementedError("render-planes S1 bead B6")
