"""Diskless node storage admission, independent from app effect authority."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MIB = 1024**2
GIB = 1024**3
OVERHEAD = 256 * MIB
EMERGENCY_HEADROOM = 512 * MIB
STORE = Path("/run/photo-wall-node-storage")
MEMINFO = Path("/proc/meminfo")
CONTROLLERS = Path("/sys/fs/cgroup/cgroup.controllers")


@dataclass(frozen=True)
class DeviceClass:
    """A board's memory class, chosen once from MemTotal: the smallest MemTotal it admits
    (below the nominal size, for kernel-reserved memory) and the node store it may mount."""

    name: str
    min_total_bytes: int
    store_bytes: int

    def __post_init__(self) -> None:
        if not (isinstance(self.name, str) and self.name
                and all(type(value) is int for value in (self.min_total_bytes, self.store_bytes))
                and 0 < self.store_bytes < self.min_total_bytes):
            raise ValueError("device_class_invalid")


# Interim values (tar era), replaced when the image format is calibrated.
CLASSES: tuple[DeviceClass, ...] = (
    DeviceClass("pi5-4gb", 3584 * MIB, 2560 * MIB),
    DeviceClass("pi5-8gb", 7168 * MIB, 3840 * MIB),
)
if not CLASSES or any(low.min_total_bytes >= high.min_total_bytes for low, high in zip(CLASSES, CLASSES[1:])):
    raise ValueError("device_class_invalid")

# The preparation slice holds the largest store plus the preparing process itself; the
# slice file's MemoryMax= is bound to this number by a test, and no unit repeats it.
PREPARATION_PROCESS_BYTES = 256 * MIB
PREPARATION_SLICE_BYTES = max(item.store_bytes for item in CLASSES) + PREPARATION_PROCESS_BYTES


class StorageShort(ValueError):
    """A refusal for memory or storage, always with its two numbers. The message is the fault
    (`node_storage_capacity` by default), so every current ValueError catcher is unchanged."""

    def __init__(self, required: int, room: int, fault: str = "node_storage_capacity"):
        super().__init__(fault)
        self.required, self.room, self.fault = required, room, fault


def memory_values(path: Path = MEMINFO) -> tuple[int, int]:
    values = {row.split(":", 1)[0]: int(row.split()[1]) * 1024 for row in path.read_text().splitlines() if row.startswith(("MemTotal:", "MemAvailable:"))}
    return values["MemTotal"], values["MemAvailable"]


def memory_total(path: Path = MEMINFO) -> int:
    try:
        rows = [row.split() for row in path.read_text().splitlines() if row.startswith("MemTotal:")]
        if len(rows) != 1 or len(rows[0]) != 3 or rows[0][2] != "kB" or not rows[0][1].isdigit():
            raise ValueError("meminfo_invalid")
        return int(rows[0][1]) * 1024
    except (OSError, UnicodeError) as error:
        raise ValueError("meminfo_invalid") from error


def memory_controller_present(path: Path = CONTROLLERS) -> bool:
    try:
        return "memory" in path.read_text().split()
    except (OSError, UnicodeError):
        return False


def device_class(total: int) -> DeviceClass:
    """The largest class this MemTotal admits; below the smallest, a refusal with its numbers."""
    admitted = [item for item in CLASSES if item.min_total_bytes <= total]
    if not admitted:
        raise StorageShort(CLASSES[0].min_total_bytes, total, "node_memory_class")
    return admitted[-1]


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
    store = device_class(total).store_bytes
    if retained_peak > store:
        raise StorageShort(retained_peak, store)
    # Tar-era admission still reads MemAvailable above the emergency headroom.
    room = min(free, available - EMERGENCY_HEADROOM)
    if incremental > room:
        raise StorageShort(incremental, max(0, room))
    return incremental


def preparation_room(*, total: int, available: int, free: int, used: int) -> int:
    """The bytes a new preparation may stage: the smaller of the device class's store left, the
    free bytes, and MemAvailable above the emergency headroom; never below 0 (a refusal needs
    `required > room`, and required always exceeds 0, so the clamp changes no decision)."""
    # The class fixes the whole-store cap. MemAvailable already excludes old root/app
    # resident pages; compare only incremental staging plus emergency headroom.
    return max(0, min(device_class(total).store_bytes - used, free, available - EMERGENCY_HEADROOM))


def admit_preparation(size_bytes: int, *, total: int, available: int, free: int,
                      used: int) -> int:
    incremental = 2 * size_bytes + OVERHEAD
    room = preparation_room(total=total, available=available, free=free, used=used)
    if incremental > room:
        raise StorageShort(incremental, room)
    return incremental
