"""The console's corner check (central/console/src/convex.js `isConvex`) against the shared Frame fit
vectors (tests/support/frame_fit_vectors.json, roadmap 1c T0) that tests/test_frame_fit.py checks
contracts/frame_fit.py `convex_clockwise` with: a quad the console accepts is one Central accepts,
and a folded one is refused in both.

Runs the module under Node (it is pure), which the console build already requires, the way
tests/test_console_flow.py runs console modules. Without Node it skips on a developer machine, but
FAILS where the checks are meant to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set).

Mutation probe: set convex.js EPSILON to -2 -> the folded vectors read convex -> RED.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from contracts.frame_fit import convex_clockwise

ROOT = Path(__file__).parents[1]
CONVEX = ROOT / "central/console/src/convex.js"
VECTORS = ROOT / "tests/support/frame_fit_vectors.json"

SCRIPT = r"""
const convex = await import(process.argv[1]);
const vectors = JSON.parse(process.argv[2]);
const quads = [...vectors.convex, ...vectors.folded, ...vectors.cover.map((vector) => vector.corners)];
process.stdout.write(JSON.stringify(quads.map((corners) => convex.isConvex(corners))));
"""


def _require_node():
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI") or os.environ.get("PHOTO_WALL_BROWSER_TESTS"):
        pytest.fail("Node (the console build's) is absent where the console checks must run")
    pytest.skip("Node (the console build's) is absent")


def test_the_console_and_central_agree_on_every_shared_corner_vector():
    _require_node()
    vectors = json.loads(VECTORS.read_text())
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", CONVEX.as_uri(), json.dumps(vectors)],
        capture_output=True, text=True, check=True, timeout=60)
    console = json.loads(result.stdout)
    quads = [*vectors["convex"], *vectors["folded"], *(vector["corners"] for vector in vectors["cover"])]
    assert console == [convex_clockwise(corners) for corners in quads]
    assert console[:len(vectors["convex"])] == [True] * len(vectors["convex"])
    folded = console[len(vectors["convex"]):len(vectors["convex"]) + len(vectors["folded"])]
    assert folded == [False] * len(vectors["folded"])
