"""Diskless node storage admission, independent from app effect authority."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from contracts.node_link import NODE_BUS_MEMORY_MAX

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

# --- the memory line table (design-r3 §2.4) ---------------------------------------------------
# One line per memory consumer on the smallest device class. Every program slice and unit
# MemoryMax= equals its line (bound by tests/node/apps/test_memory_lines.py); a line with a
# reading follows the rule, checked when the line is built, so a wrong cap fails at import.

MEMORY_ROUND: Final = 32 * MIB


def cap_from_peak(peak_bytes: int, floor_bytes: int = 0) -> int:
    """floor + max(2 * peak, peak + 32 MiB), rounded up to MEMORY_ROUND (design-r3 §2.4 rule 3)."""
    raw = floor_bytes + max(2 * peak_bytes, peak_bytes + 32 * MIB)
    return -(-raw // MEMORY_ROUND) * MEMORY_ROUND


@dataclass(frozen=True, slots=True)
class MemoryLine:
    """One consumer's memory line. `basis` names the reading (M), spike (S) or estimate (E) the
    cap rests on; `peak_bytes` is the peak a measured cap follows; `floor_bytes` a configured
    budget the cap never goes below; `cgroup` the unit or slice file whose MemoryMax= is this
    line; `parent` the line of the slice that cgroup sits in."""

    name: str
    cap_bytes: int
    basis: str
    peak_bytes: int | None = None
    floor_bytes: int = 0
    cgroup: str | None = None
    parent: str | None = None

    def __post_init__(self) -> None:
        if self.cap_bytes < 1 or (self.peak_bytes is not None
                                  and self.cap_bytes != cap_from_peak(self.peak_bytes, self.floor_bytes)):
            raise ValueError("memory_line_invalid")


LINES: Final[tuple[MemoryLine, ...]] = (
    MemoryLine("kernel", 200 * MIB, "E: kernel, slab, page tables, PID1, journald, udev"),
    MemoryLine("cma", 64 * MIB, "E"),
    MemoryLine("base-image", 320 * MIB, "M 311, image size"),
    MemoryLine("overlay", 128 * MIB, "E use; the 512 MiB mount size is unchanged (§2.4 rule 6)"),
    MemoryLine("hostcore", 96 * MIB, "M 26 + S 8", peak_bytes=34 * MIB, cgroup="photowallhostcore.slice"),
    MemoryLine("host-core", 96 * MIB, "the slice's whole line",
               cgroup="photo-wall-host-core.service", parent="hostcore"),
    MemoryLine("base", 480 * MIB, "M 138 + S/E 90", peak_bytes=228 * MIB, cgroup="photowallbase.slice"),
    MemoryLine("broker", 96 * MIB, "E inside the measured slice (W4)",
               cgroup="photo-wall-app-broker.service", parent="base"),
    MemoryLine("manager-supervisor", 96 * MIB, "E (W4); leaves with AppManager",
               cgroup="photo-wall-manager-supervisor.service", parent="base"),
    MemoryLine("display", 64 * MIB, "M 9", peak_bytes=9 * MIB,
               cgroup="photo-wall-display.service", parent="base"),
    MemoryLine("display-controller", 64 * MIB, "E (W4)",
               cgroup="photo-wall-display-controller.service", parent="base"),
    MemoryLine("health", 64 * MIB, "E (W4); unchanged", cgroup="photo-wall-health.service", parent="base"),
    # The bus's own slice, outside the base slice (E-E3C-CUT-6), and persistent, so its counters
    # survive the bus's restarts (erratum E-E3C-S5-3); the unit is its one member.
    MemoryLine("bus", NODE_BUS_MEMORY_MAX,
               "contracts.node_link fit (E3b design §7.2); measured by checks.yml bus-fence",
               cgroup="photowallbus.slice"),
    MemoryLine("bus-server", NODE_BUS_MEMORY_MAX, "the slice's whole line",
               cgroup="photo-wall-bus.service", parent="bus"),
    MemoryLine("app", 992 * MIB, "M 229; texture budget 512 (player/service.py:128)",
               peak_bytes=229 * MIB, floor_bytes=512 * MIB, cgroup="photowallapp.slice"),
    MemoryLine("preparation", PREPARATION_SLICE_BYTES, "interim, tar era: PREPARATION_SLICE_BYTES; "
               "E2c sets it to the root lines plus the process line", cgroup="photowallpreparation.slice"),
)
_LINES_BY_NAME: Final = {item.name: item for item in LINES}
# Every service line sits in a slice line of this table, never in PID1's default system.slice: a
# unit's own cgroup is recreated at each restart, so only a slice keeps a cumulative reading
# (memory.events, memory.peak) across one (erratum E-E3C-S5-3).
_SLICE_LINES: Final = {item.name for item in LINES if (item.cgroup or "").endswith(".slice")}
if len(_LINES_BY_NAME) != len(LINES) or any(
        item.cgroup is not None and item.name not in _SLICE_LINES and item.parent not in _SLICE_LINES
        for item in LINES):
    raise ValueError("memory_line_invalid")


def line(name: str) -> MemoryLine:
    """The line called `name`; KeyError on an unknown name."""
    return _LINES_BY_NAME[name]


def cgroup_path(name: str) -> str:
    """The line's cgroup directory under /sys/fs/cgroup: a slice line's slice (every program slice
    is top-level), a member's `<its slice>/<its unit>`; KeyError on an unknown or cgroup-less line."""
    item = line(name)
    if item.cgroup is None:
        raise KeyError(name)
    return item.cgroup if item.parent is None else f"{line(item.parent).cgroup}/{item.cgroup}"


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
