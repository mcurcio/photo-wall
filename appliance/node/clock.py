"""Linux boot identity and suspend-aware time, with no effect dependencies."""
from __future__ import annotations

import time
from pathlib import Path
from uuid import UUID


def boot_id(proc: Path = Path("/proc")) -> UUID:
    return UUID((proc / "sys/kernel/random/boot_id").read_text().strip())


def boottime_ms() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000

