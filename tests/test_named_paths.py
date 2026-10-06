"""Every path a test, a script, a workflow cache key or the display meson list names exists.

A negative assertion about a path that no longer exists passes vacuously, and a `hashFiles`
pattern that matches nothing hashes the empty set into a cache key that never changes. Both
fail silently after a file moves; these guards make them fail loudly.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Final

from scripts.release_plan import matches

REPO: Final = Path(__file__).resolve().parents[1]
SCANNED: Final = ("tests", "scripts")
WORKFLOWS: Final = REPO / ".github" / "workflows"
MESON: Final = REPO / "appliance" / "display_host" / "meson.build"
OVERLAY: Final = "appliance/display_host/overlay"
KNOWN_ABSENT: Final[frozenset[str]] = frozenset({
    "appliance/build.py", "appliance/updates.py", "appliance/sub/provision.py"})  # deliberate fixtures
APPLIANCE_PATH: Final = re.compile(r"(?<![\w.])(appliance/(?:[a-z_]+/)*[a-z_]+\.py)\b")
HASHED: Final = re.compile(r"hashFiles\(([^)]*)\)")
QUOTED: Final = re.compile(r"'([^']*)'")
MESON_OVERLAY: Final = re.compile(r"'(overlay/[^'/]+\.py)'")


@cache
def _tracked() -> frozenset[str]:
    """Every file the next commit would hold: tracked or new, not ignored, not deleted."""
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], check=True, capture_output=True,
                            text=True).stdout.strip("\0").split("\0")
    return frozenset(path for path in listed if (REPO / path).is_file())


def appliance_path_literals(path: Path) -> Iterator[tuple[int, str]]:
    """(line, path) for each `appliance/...py` path inside a str constant of `path`."""
    for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in APPLIANCE_PATH.finditer(node.value):
                yield node.lineno, match.group(1)


def test_every_appliance_path_literal_in_tests_and_scripts_names_a_tracked_file() -> None:
    literals = [(path.relative_to(REPO).as_posix(), line, literal)
                for directory in SCANNED
                for path in sorted((REPO / directory).rglob("*.py"))
                if "node_modules" not in path.parts
                for line, literal in appliance_path_literals(path)]
    assert literals
    missing = [f"{path}:{line}: {literal}" for path, line, literal in literals
               if literal not in _tracked() and literal not in KNOWN_ABSENT]
    assert not missing, missing


def test_every_hashed_pattern_in_a_workflow_matches_a_tracked_file() -> None:
    patterns = [(workflow.name, pattern)
                for workflow in sorted(WORKFLOWS.glob("*.yml"))
                for call in HASHED.finditer(workflow.read_text())
                for pattern in QUOTED.findall(call.group(1))]
    assert patterns
    empty = [f"{workflow}: {pattern}" for workflow, pattern in patterns
             if not any(matches(pattern, path) for path in _tracked())]
    assert not empty, empty


def test_the_overlay_install_list_is_every_overlay_module() -> None:
    installed = {f"appliance/display_host/{name}"
                 for name in MESON_OVERLAY.findall(MESON.read_text())}
    tracked = {path for path in _tracked()
               if path.startswith(OVERLAY + "/") and path.endswith(".py")
               and "/" not in path.removeprefix(OVERLAY + "/")}
    assert installed == tracked
