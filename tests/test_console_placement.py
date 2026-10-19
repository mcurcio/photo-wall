"""The console's placement rule (central/console/src/placement.js `hanging`, `placeBox`,
`boxSizeMm`) against Central's, on vectors built here (render redesign S5, run ledger
.claude/runs/render-planes/s5-page.md B2; frame box run, .claude/runs/frame-box/page.md fb-4, fb-7):
`hanging` against contracts/display_turn.py `hanging`; `placeBox` against contracts/frame_fit.py
`place_box` (with `FrameBox`'s check) and `boxSizeMm` against contracts/models.py `frame_size_mm`,
on fb-1's grids and on 2,000 seeded random boxes, refusals included. The rectangle rule (`place`,
`pixels`, `fillScreen`) went with the rectangle editor (fb-7): the module exports nothing else.

Runs the module under Node (it is pure), which the console build already requires, the way
tests/test_console_flow.py runs console modules. Without Node it skips on a developer machine, but
FAILS where the checks are meant to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set).

Mutation probe: compute placeBox's width as max(1, halfUp(width * W)) instead of from its two
edges -> the box grid -> RED.
"""

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from contracts.display_turn import TURNS, hanging
from contracts.frame_fit import FrameBox, place_box
from contracts.models import PositionBox, frame_size_mm

ROOT = Path(__file__).parents[1]
PLACEMENT = ROOT / "central/console/src/placement.js"

# The screens `hanging` is checked on (modes as built).
OUTPUTS = [(3840, 2160), (1920, 1080), (1080, 1920), (1280, 1024)]

SCRIPT = r"""
const placement = await import(process.argv[1]);
let text = "";
for await (const chunk of process.stdin) text += chunk;
const cases = JSON.parse(text);
const run = (call) => {
  try {
    return { value: call() };
  } catch (error) {
    return { error: error.message };
  }
};
const out = cases.map(([name, args]) => run(() => placement[name](...args)));
process.stdout.write(JSON.stringify(out));
"""


def _require_node():
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI") or os.environ.get("PHOTO_WALL_BROWSER_TESTS"):
        pytest.fail("Node (the console build's) is absent where the console checks must run")
    pytest.skip("Node (the console build's) is absent")


def _console(cases):
    """Each (function name, args) case's answer in placement.js: {"value": ...} or {"error": code}."""
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", PLACEMENT.as_uri()],
        input=json.dumps(cases), capture_output=True, text=True, check=True, timeout=120)
    return json.loads(result.stdout)


def _python_hanging(size, turn):
    try:
        return list(hanging(tuple(size), turn))
    except ValueError as error:
        return {"error": str(error)}


def _answer(answer):
    return {"error": answer["error"]} if "error" in answer else answer["value"]


def test_hanging_matches_central_on_every_grid_screen_at_every_turn_and_refuses_alike():
    cases = [[list(size), turn] for size in OUTPUTS for turn in TURNS]
    cases += [[[1920, 1080], 45], [[1920, 1080], 360], [[1920, 1080], -90], [[1920, 1080], True],
              [[0, 1080], 0], [[1920, 0], 90], [[1920.5, 1080], 0], [[-1, 1080], 180], [[1920, True], 0]]
    console = [_answer(answer) for answer in _console([["hanging", case] for case in cases])]
    assert console == [_python_hanging(*case) for case in cases]
    assert console[1] == [2160, 3840]


def test_the_module_keeps_only_the_screens_turn_and_the_box():
    """The rectangle rule went with the rectangle editor (frame box run fb-7): `place`, `pixels` and
    `fillScreen` are no longer the console's."""
    _require_node()
    script = "const m = await import(process.argv[1]); process.stdout.write(JSON.stringify(Object.keys(m).sort()));"
    result = subprocess.run(["node", "--input-type=module", "-e", script, "--", PLACEMENT.as_uri()],
                            capture_output=True, text=True, check=True, timeout=120)
    assert json.loads(result.stdout) == ["boxSizeMm", "hanging", "placeBox"]


# fb-1's grids (tests/test_frame_box.py): the screens and the steps a box's edges take.
BOX_SCREENS = [(1, 1), (7, 3), (1920, 1080), (1080, 1920), (3840, 2160)]
BOX_STEPS = sorted({round(i * 0.05, 10) for i in range(21)} | {0.0, 1e-6, 1.0})
BOX_KEYS = ("left", "top", "width", "height")


def _python_place_box(box, screen):
    """Central's answer for a box in the console's shape: a placement dict or {"error": code}. The
    box is a FrameBox before it is placed, so its refusal comes first, as in the console."""
    try:
        placed = place_box(FrameBox(**box), tuple(screen))
    except ValueError as error:
        return {"error": str(error)}
    return {"x": placed.x, "y": placed.y, "width": placed.width, "height": placed.height}


def _python_box_size(box, display_mm, turn):
    """Central's `frame_size_mm` for a box in the console's shape: [width, height], None, or
    {"error": code} (a box that is no FrameBox is refused "frame_box" before any size)."""
    try:
        FrameBox(**box)
        size = frame_size_mm(PositionBox(**box), None if display_mm is None else tuple(display_mm), turn)
    except ValueError as error:
        return {"error": str(error)}
    return None if size is None else list(size)


def _grid_boxes():
    """fb-1's totality grid, each box both at the top-left diagonal and flush with the bottom."""
    boxes = []
    for left in BOX_STEPS:
        for width in BOX_STEPS:
            boxes.append({"left": left, "top": left, "width": width, "height": width})
            boxes.append({"left": left, "top": round(1 - width, 10), "width": width, "height": width})
    return boxes


def _random_box(rng):
    """Half the time a box on its screen (any shape, flush edges and tiny sides among them), else one
    near and past the screen's edges: exact edges, whole numbers and refusals."""
    if rng.random() < 0.5:
        left, top = rng.uniform(0, 1), rng.uniform(0, 1)
        return {"left": left, "top": top,
                "width": rng.choice([1 - left, rng.uniform(0, 1 - left), 1e-6]) or 1e-6,
                "height": rng.choice([1 - top, rng.uniform(0, 1 - top), 1e-6]) or 1e-6}

    def side():
        return rng.choice([rng.uniform(-0.05, 1.1), rng.uniform(0, 1), rng.uniform(0, 1e-3),
                           0.0, 1.0, 1e-6, 0.5, -1e-6, rng.randint(0, 2), True])

    box = {key: side() for key in BOX_KEYS}
    if rng.random() < 0.3 and isinstance(box["left"], float):
        box["width"] = 1 - box["left"]
    if rng.random() < 0.3 and isinstance(box["top"], float):
        box["height"] = 1 - box["top"]
    return box


def _random_screen(rng):
    return rng.choice([[rng.randint(1, 4000), rng.randint(1, 4000)], [rng.randint(1, 9), rng.randint(1, 9)],
                       [1920, 1080], [1080, 1920], [0, 1080], [1920, 1080.5], [-3, 7], [1920, True]])


def _random_display_mm(rng):
    return rng.choice([None, [rng.randint(1, 2000), rng.randint(1, 2000)], [950, 540], [0, 540],
                       [950.5, 540], [True, 540]])


def test_place_box_matches_central_on_fb1s_grids():
    cases = [(box, list(screen)) for screen in BOX_SCREENS for box in _grid_boxes()]
    cases += [({"left": x / w, "top": y / h, "width": pw / w, "height": ph / h}, [w, h])
              for w, h in [(1, 1), (7, 3), (64, 36), (36, 64)]
              for x in range(w) for pw in range(1, w - x + 1)
              for y in range(0, h, max(1, h // 6)) for ph in (1, h - y)]
    console = [_answer(answer) for answer in _console([["placeBox", list(case)] for case in cases])]
    central = [_python_place_box(*case) for case in cases]
    assert [(case, js, py) for case, js, py in zip(cases, console, central, strict=True) if js != py] == []
    # The grid holds both answers, so neither side passes by always refusing.
    assert any("error" in answer for answer in central) and any("error" not in answer for answer in central)


def test_place_box_and_box_size_match_central_on_2000_random_boxes():
    rng = random.Random(20261019)
    places = [(_random_box(rng), _random_screen(rng)) for _ in range(2000)]
    sizes = [(_random_box(rng), _random_display_mm(rng), rng.choice([0, 90, 180, 270, 0, 90, 45]))
             for _ in range(2000)]
    console = [_answer(answer) for answer in _console(
        [["placeBox", list(case)] for case in places] + [["boxSizeMm", list(case)] for case in sizes])]
    central = [_python_place_box(*case) for case in places] + [_python_box_size(*case) for case in sizes]
    cases = places + sizes
    assert [(case, js, py) for case, js, py in zip(cases, console, central, strict=True) if js != py] == []
    # Every refusal and every kind of answer is met, so neither side passes by one answer alone.
    codes = {answer["error"] for answer in central if isinstance(answer, dict) and "error" in answer}
    assert codes == {"frame_box", "placement_screen", "screen_size", "display_turn"}
    assert sum(1 for answer in central if isinstance(answer, dict) and "x" in answer) > 300
    assert sum(1 for answer in central if isinstance(answer, list)) > 300
    assert any(answer is None for answer in central)


def test_box_size_matches_central_on_fb1s_grid_at_every_turn():
    cases = [(box, [950, 540], turn) for box in _grid_boxes() for turn in TURNS]
    cases.append(({"left": 0, "top": 0, "width": 0.5, "height": 0.25}, None, 90))
    console = [_answer(answer) for answer in _console([["boxSizeMm", list(case)] for case in cases])]
    assert console == [_python_box_size(*case) for case in cases]
    (quarter,) = _console([["boxSizeMm", [{"left": 0, "top": 0, "width": 0.5, "height": 0.25}, [950, 540], 90]]])
    assert quarter["value"] == [270, 237.5]
