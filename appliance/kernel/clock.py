"""Linux boot identity and suspend-aware time, with no effect dependencies."""
from __future__ import annotations

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
    raise NotImplementedError
