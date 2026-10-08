"""The memory line table (appliance/kernel/capacity.py LINES) against the node's systemd files and
the transient Player unit: every MemoryMax= is its line, members fit their slice, MemoryMin=
stays inside its line. Config-file test: no systemd, no database."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Final

from support.repo import REPO
from test_netboot_liveness import _parse_unit

from appliance.apps.process_linux import app_unit_properties
from appliance.kernel.capacity import GIB, LINES, MIB, line

SYSTEMD = REPO / "appliance/systemd"
# The unit files node-base-deb ships (scripts/release_plan.py, its systemd globs).
NODE_UNITS: Final = tuple(sorted((*SYSTEMD.glob("photo-wall-*.service"), *SYSTEMD.glob("photowall*.slice"))))
SIZES = {"K": 1024, "M": MIB, "G": GIB}


def _bytes(value: str) -> int:
    return int(value[:-1]) * SIZES[value[-1]] if value[-1] in SIZES else int(value)


def _memory(path: Path, key: str) -> list[int]:
    """Every `key=` value the file's [Service] or [Slice] section sets, in bytes."""
    sections = _parse_unit(path.read_text())
    return [_bytes(value) for section in ("Service", "Slice") for value in sections.get(section, {}).get(key, [])]


def test_every_node_unit_cap_is_its_line() -> None:
    by_cgroup = {item.cgroup: item for item in LINES if item.cgroup is not None}
    capped = {path.name: _memory(path, "MemoryMax") for path in NODE_UNITS if _memory(path, "MemoryMax")}
    assert capped, "no node unit sets MemoryMax="
    for name, values in capped.items():
        assert name in by_cgroup, f"{name} sets MemoryMax= but no line names it"
        assert values == [by_cgroup[name].cap_bytes], (name, values, by_cgroup[name])
    names = {path.name for path in NODE_UNITS}
    for cgroup, item in by_cgroup.items():
        assert cgroup in names, f"line {item.name} names {cgroup}, which is not a node unit file"
        assert capped.get(cgroup) == [item.cap_bytes], (item, capped.get(cgroup))


def test_members_fit_inside_their_slice() -> None:
    members: dict[str, int] = defaultdict(int)
    for item in LINES:
        if item.parent is not None:
            members[item.parent] += item.cap_bytes
    assert members, "no line sits inside a slice"
    for parent, total in members.items():
        assert total <= line(parent).cap_bytes, (parent, total // MIB, line(parent).cap_bytes // MIB)


def test_every_memory_min_is_within_its_line() -> None:
    by_cgroup = {item.cgroup: item for item in LINES if item.cgroup is not None}
    protected = {path.name: _memory(path, "MemoryMin") for path in NODE_UNITS if _memory(path, "MemoryMin")}
    assert protected, "no node unit sets MemoryMin="
    for name, values in protected.items():
        assert name in by_cgroup, f"{name} sets MemoryMin= but no line names it"
        assert all(value <= by_cgroup[name].cap_bytes for value in values), (name, values, by_cgroup[name])


def test_the_transient_player_unit_cap_is_the_app_line() -> None:
    caps = [p for p in app_unit_properties(Path("/r")) if p.startswith("MemoryMax=")]
    assert caps == [f"MemoryMax={line('app').cap_bytes}"], caps
