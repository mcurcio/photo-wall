"""Diskless node storage admission, independent from app effect authority."""
from __future__ import annotations

from pathlib import Path

GIB = 1024**3
RESERVE = 4 * GIB  # App 2GiB, other base 768MiB, HostCore96MiB, kernel/base/overlay overhead.
MAX_STORE = 4 * GIB
MIN_MEMORY = 7 * GIB  # Allows kernel-reserved memory on an 8GiB device.
OVERHEAD = 256 * 1024**2
EMERGENCY_HEADROOM = 512 * 1024**2
STORE = Path("/run/photo-wall-node-storage")


def memory_values(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    values = {row.split(":", 1)[0]: int(row.split()[1]) * 1024 for row in path.read_text().splitlines() if row.startswith(("MemTotal:", "MemAvailable:"))}
    return values["MemTotal"], values["MemAvailable"]


def storage_budget(total: int, available: int) -> int:
    if total < MIN_MEMORY or available <= EMERGENCY_HEADROOM:
        raise ValueError("node_storage_memory_envelope")
    return min(MAX_STORE, total - RESERVE)


def cold_peak(references) -> int:
    # Plain tar bytes upper-bound expanded regular bytes; reserve metadata/inodes.
    sizes = {ref.environment_sha256: ref.size_bytes for ref in references if ref is not None}
    return sum(sizes.values()) + max(sizes.values(), default=0) + OVERHEAD


def admit_cold(references, *, total: int, available: int, free: int,
               resident: frozenset[str] = frozenset()) -> int:
    references = tuple(references)
    sizes = {ref.environment_sha256: ref.size_bytes for ref in references if ref is not None}
    missing = [size for digest, size in sizes.items() if digest not in resident]
    incremental = sum(missing) + max(missing, default=0) + (OVERHEAD if missing else 0)
    retained_peak = sum(sizes.values()) + max(missing, default=0) + OVERHEAD
    if retained_peak > storage_budget(total, available) or incremental > min(free, available - EMERGENCY_HEADROOM):
        raise ValueError("node_storage_capacity")
    return incremental


def admit_preparation(size_bytes: int, *, total: int, available: int, free: int,
                      used: int) -> int:
    incremental = 2 * size_bytes + OVERHEAD
    # The global reserve determines the whole-store cap once. MemAvailable already
    # excludes old root/app resident pages; compare only incremental staging plus
    # emergency headroom, never subtract the full reserve a second time.
    if used + incremental > storage_budget(total, available) or incremental > min(free, available - EMERGENCY_HEADROOM):
        raise ValueError("node_storage_capacity")
    return incremental

