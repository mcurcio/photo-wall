"""No module of the Player reads this machine's wall clock.

The Player's time is Central's, carried forward on its own monotonic clock (player/show_clock.py).
A Pi's wall clock is wrong after a boot without a time source, and steps when one appears: read
anywhere in the package, it would bring back the 2026-11-08 black Frame. Mutation probe: restore
`self.clock.utc()` in player/service.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PLAYER = Path(__file__).resolve().parents[1] / "player"


def _wall_clock_reads(source: str) -> list[int]:
    tree = ast.parse(source)
    bare_time = any(
        isinstance(node, ast.ImportFrom) and node.module == "time"
        and any(alias.name == "time" for alias in node.names)
        for node in ast.walk(tree)
    )
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Attribute) and (
                called.attr == "utc"
                or (called.attr == "time" and isinstance(called.value, ast.Name)
                    and called.value.id == "time")):
            lines.append(node.lineno)
        elif bare_time and isinstance(called, ast.Name) and called.id == "time":
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("source", [
    "self.clock.utc()", "time.time()", "from time import time\ntime()",
])
def test_the_check_sees_each_way_of_reading_the_wall_clock(source):
    assert _wall_clock_reads(source)


@pytest.mark.parametrize("source", ["time.monotonic()", "self.clock.monotonic()", "time(1)"])
def test_the_check_leaves_the_monotonic_clock_alone(source):
    assert not _wall_clock_reads(source)


def test_no_player_module_reads_the_wall_clock():
    found = {
        path.name: reads
        for path in sorted(PLAYER.rglob("*.py"))
        if (reads := _wall_clock_reads(path.read_text()))
    }
    assert not found, f"the Player read the wall clock at {found}"
