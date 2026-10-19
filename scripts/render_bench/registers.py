"""The display controller's glitch evidence (bead B6): the pixel valve's sticky status bits (bench T2;
the kernel's hvs_underrun counter is dead on the Pi 5), read from /dev/mem as root. They stay set
until the next full modeset, so ten reads a second miss none."""
from __future__ import annotations

import ctypes
import mmap
import os
import time
from dataclasses import dataclass
from typing import Final

PV0_BASE: Final = 0x107C410000      # pixel valve 0 (HDMI-A-1), test2/src/hvsbench.c
PV_STATUS: Final = 0x2C             # its status register
STICKY_GLITCH: Final = 0xA00        # the bits an underrun sets and leaves set
_MAP_FAILED: Final = ctypes.c_void_p(-1).value


@dataclass(frozen=True)
class RegisterSample:
    """One read of the pixel valve's status at monotonic `at`."""
    at: float
    pv_status: int


def glitched(first: RegisterSample, sample: RegisterSample) -> bool:
    """Whether a STICKY_GLITCH bit is set in `sample` that was clear in `first`. Pure."""
    return bool(sample.pv_status & STICKY_GLITCH & ~first.pv_status)


def blind(first: RegisterSample) -> bool:
    """Whether `first` already has every STICKY_GLITCH bit set, so no later glitch can show (only a
    full modeset clears them: reboot before the run). Pure."""
    return first.pv_status & STICKY_GLITCH == STICKY_GLITCH


class RegisterWatch:
    """Reads the pixel valve's status through a read-only mapping of /dev/mem."""

    def __init__(self, memory: str = "/dev/mem") -> None:
        """Map PV0_BASE read-only. PermissionError when not root."""
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int64)
        self._libc.mmap.restype = ctypes.c_void_p
        self._libc.munmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        self._page = mmap.PAGESIZE
        base = PV0_BASE & ~(self._page - 1)
        self._offset = PV0_BASE - base + PV_STATUS
        self._fd = os.open(memory, os.O_RDONLY | os.O_SYNC | os.O_CLOEXEC)
        address = self._libc.mmap(None, self._page, mmap.PROT_READ, mmap.MAP_SHARED, self._fd, base)
        if address in (None, _MAP_FAILED):
            error = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(error, "pv_map")
        self._address: int | None = address

    def sample(self) -> RegisterSample:
        """The status now (one 32-bit load)."""
        if self._address is None:
            raise RuntimeError("register_watch_closed")
        return RegisterSample(time.monotonic(), ctypes.c_uint32.from_address(self._address + self._offset).value)

    def close(self) -> None:
        """Unmap."""
        if self._address is not None:
            self._libc.munmap(self._address, self._page)
            os.close(self._fd)
            self._address = None


__all__ = ["PV0_BASE", "PV_STATUS", "STICKY_GLITCH", "RegisterSample", "RegisterWatch", "blind", "glitched"]
