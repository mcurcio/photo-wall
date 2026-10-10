"""How many file descriptors the app run holds against its soft limit (1b P1b).

The broker measures the Player from outside, so a Player that has run out of descriptors (and
cannot open a socket to say so) is still measured. The broker runs as root with CAP_SYS_PTRACE
(photo-wall-app-broker.service), which reading another uid's /proc/<pid>/fd needs; it already
relies on the same access for /proc/<pid>/root (`process_linux.process_root_matches`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

# How often the broker's main loop samples the current run (one listdir and one small read).
DESCRIPTOR_SAMPLE_MS: Final = 10_000


@dataclass(frozen=True, slots=True)
class DescriptorUse:
    open: int  # entries in /proc/<pid>/fd
    soft_limit: int  # the soft "Max open files" of /proc/<pid>/limits

    def __post_init__(self) -> None:
        """ValueError("descriptor_use") unless both are ints, open >= 0 and soft_limit >= 1."""
        raise NotImplementedError


def parse_soft_limit(limits: str) -> int:
    """PURE. The soft limit of the "Max open files" row of a /proc/<pid>/limits text (columns:
    name, soft, hard, units). ValueError("limits_unreadable") when the row is missing, appears
    twice, or its soft value is not a decimal integer >= 1 ("unlimited" included: RLIMIT_NOFILE
    cannot be unlimited)."""
    raise NotImplementedError


def descriptor_use(proc: Path, pid: int, start_ticks: int) -> DescriptorUse | None:
    """The run's use, or None when the process is gone or `pid` now names another process (its
    kernel birth, `appliance.process_identity.read_proc_start_ticks`, differs from `start_ticks`
    before or after the reads), so a reused pid is never measured. A read refused for any other
    reason raises OSError (the caller skips the sample)."""
    raise NotImplementedError


__all__ = ["DESCRIPTOR_SAMPLE_MS", "DescriptorUse", "descriptor_use", "parse_soft_limit"]
