"""The memory line table (appliance/kernel/capacity.py LINES) against the node's systemd files and
the transient Player unit: every MemoryMax= is its line, members fit their slice, MemoryMin=
stays inside its line; the content line left on every class, and each shipped image within its
line. Config-file test: no systemd, no database."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from support.repo import REPO
from test_netboot_liveness import _parse_unit

from appliance.apps.process_linux import app_unit_properties
from appliance.kernel.capacity import (
    CLASSES,
    GIB,
    LINES,
    MIB,
    STORE_BYTES,
    cgroup_path,
    check_content_line,
    content_line,
    line,
)
from scripts.build_node_base_deb import UNITS
from scripts.build_node_components import check_image_lines

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


def test_every_line_unit_ships_and_sits_in_its_lines_slice() -> None:
    # The base ships every line's file (a slice PID1 made up from a Slice= alone would carry no cap),
    # and a member unit's Slice= is its parent line's slice, so cgroup_path is where PID1 puts it.
    for item in (entry for entry in LINES if entry.cgroup is not None):
        assert item.cgroup in UNITS, f"line {item.name}: node-base-deb does not ship {item.cgroup}"
        if item.parent is not None:
            unit = _parse_unit((SYSTEMD / item.cgroup).read_text())
            assert unit["Service"]["Slice"] == [line(item.parent).cgroup], (item, unit["Service"].get("Slice"))
            assert cgroup_path(item.name) == f"{line(item.parent).cgroup}/{item.cgroup}"


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


def test_the_content_line_is_what_a_board_holds_beyond_the_fixed_lines() -> None:
    # Design-r3 §2.4 after E2c (brief §3(b)): fixed 3496 MiB; the measured 4 GB board and the class floor.
    assert content_line(4045 * MIB) == 549 * MIB
    assert content_line(3584 * MIB) == 88 * MIB
    assert line("preparation").cap_bytes == 960 * MIB
    assert STORE_BYTES == 768 * MIB


def test_a_line_table_that_leaves_no_content_line_is_refused() -> None:
    check_content_line(LINES, CLASSES)
    floor = min(item.min_total_bytes for item in CLASSES)
    # Raise the app line until the smallest class has no content left, then one byte short of it.
    exhausted = tuple(replace(item, cap_bytes=item.cap_bytes + content_line(floor), peak_bytes=None)
                      if item.name == "app" else item for item in LINES)
    with pytest.raises(ValueError, match="^memory_line_no_content$"):
        check_content_line(exhausted, CLASSES)
    one_left = tuple(replace(item, cap_bytes=item.cap_bytes - 1) if item.name == "app" else item
                     for item in exhausted)
    check_content_line(one_left, CLASSES)


def test_a_shipped_image_over_its_line_fails_the_component_build() -> None:
    check_image_lines({"app": line("app-image").cap_bytes, "manager-primary": line("manager-image").cap_bytes})
    for sizes in ({"app": 321 * MIB}, {"manager-primary": line("manager-image").cap_bytes + 1}):
        with pytest.raises(ValueError, match="^node_components_image_over_line$"):
            check_image_lines(sizes)
