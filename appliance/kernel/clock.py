"""Linux boot identity and suspend-aware time, with no effect dependencies."""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import UUID


def boot_id(proc: Path = Path("/proc")) -> UUID:
    return UUID((proc / "sys/kernel/random/boot_id").read_text().strip())


def boottime_ms() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000


WallClockState = Literal["synchronised", "unsynchronised"]
"""The wall clock as the kernel judges it (decision 0032, slice A): "synchronised" while a time
client disciplines it (adjtimex(2) answers without TIME_ERROR and STA_UNSYNC is clear); else
"unsynchronised", whatever its reading. Central is never an input."""


def wall_clock_state(adjtimex: Callable[[], tuple[int, int]] | None = None) -> WallClockState | None:
    """The kernel's verdict on the wall clock, read now: the one judge of wall time on a Node.
    `adjtimex` returns (the call's return value, `timex.status`) for a read-only call (modes 0);
    the default calls libc's adjtimex through ctypes. None when it cannot be read (not Linux, or the
    call fails): unknown, never a guess. Read where it is reported, never cached."""
    try:
        result, status = (adjtimex or _libc_adjtimex)()
    except (OSError, AttributeError):
        return None
    if result == _TIME_ERROR or status & _STA_UNSYNC:
        return "unsynchronised"
    return "synchronised"


_TIME_ERROR = 5             # adjtimex(2): the clock is not synchronised
_STA_UNSYNC = 0x0040        # timex.status: the clock is not synchronised
_TIMEX_BYTES = 208          # sizeof(struct timex) on LP64 (aarch64)
_TIMEX_STATUS_OFFSET = 40   # offsetof(struct timex, status) on LP64


def _libc_adjtimex() -> tuple[int, int]:
    """libc's adjtimex on a zeroed `struct timex` (modes 0: read only). OSError when the call
    fails, AttributeError where libc has no adjtimex (not Linux)."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.adjtimex
    buffer = ctypes.create_string_buffer(_TIMEX_BYTES)
    result = function(buffer)
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result, int.from_bytes(buffer.raw[_TIMEX_STATUS_OFFSET:_TIMEX_STATUS_OFFSET + 4],
                                  sys.byteorder, signed=True)
