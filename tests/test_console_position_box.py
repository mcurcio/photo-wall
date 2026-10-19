"""The console's box in the operator's terms (central/console/src/domain/position-box.ts; frame box
run, .claude/runs/frame-box/page.md fb-4): moving and resizing a box by whole pixels of its screen,
its pixels and back, and its shape and size in words. Bundled by the console's own esbuild and run
under Node (the module is pure), as tests/test_console_refusal_words.py runs console modules.
Without Node or esbuild it skips on a developer machine, but FAILS where the checks are meant to run
in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set).

Mutation probe: in `resizedBy` move the left edge for a "right" drag -> the right-edge resize case
-> RED.
"""

import json
import subprocess

import pytest
from test_console_flow import _require_node
from test_console_routes_r4 import ESBUILD, SRC, _require_esbuild

from contracts.frame_fit import FrameBox, Placement, place_box

SCRIPT = """
import * as box from {module};
let text = "";
for await (const chunk of process.stdin) text += chunk;
const run = (call) => {{
  try {{
    return {{ value: call() }};
  }} catch (error) {{
    return {{ error: error.message }};
  }}
}};
const out = JSON.parse(text).map(([name, args]) => run(() => box[name](...args)));
process.stdout.write(JSON.stringify({{ whole: box.WHOLE_SCREEN, out }}));
"""

SCREEN = [1920, 1080]
# A box a third of the way in, 960 x 540 pixels of SCREEN at (480, 270).
QUARTER = {"left": 0.25, "top": 0.25, "width": 0.5, "height": 0.5}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    _require_node()
    _require_esbuild()
    folder = tmp_path_factory.mktemp("position-box")
    entry = folder / "entry.ts"
    entry.write_text(SCRIPT.format(module=json.dumps(str(SRC / "domain/position-box.ts"))))
    built = folder / "entry.mjs"
    subprocess.run([str(ESBUILD), str(entry), "--bundle", "--format=esm", "--platform=node", f"--outfile={built}",
                    "--log-level=error"], check=True, timeout=60)
    return built


@pytest.fixture(scope="module")
def call(bundle):
    def answers(cases):
        result = subprocess.run(["node", str(bundle)], input=json.dumps(cases), capture_output=True, text=True,
                                check=True, timeout=60)
        out = json.loads(result.stdout)["out"]
        return [answer["value"] if "value" in answer else {"error": answer.get("error")} for answer in out]
    return answers


def _one(call, name, *args):
    (answer,) = call([[name, list(args)]])
    return answer


def _pixels(call, box, screen=SCREEN):
    return _one(call, "boxPixels", box, screen)


def test_the_whole_screen_is_a_new_frames_box(bundle):
    result = subprocess.run(["node", str(bundle)], input="[]", capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(result.stdout)["whole"] == {"left": 0, "top": 0, "width": 1, "height": 1}


def test_the_screen_as_it_hangs_or_none(call):
    assert _one(call, "screenOf", [1920, 1080], 0) == [1920, 1080]
    assert _one(call, "screenOf", [1920, 1080], 90) == [1080, 1920]
    assert _one(call, "screenOf", None, 270) is None


def test_box_pixels_are_centrals_placement(call):
    for box in (QUARTER, {"left": 0.7, "top": 0.7, "width": 0.3, "height": 0.3},
                {"left": 0, "top": 0, "width": 1, "height": 1}):
        placed = place_box(FrameBox(**box), tuple(SCREEN))
        assert _pixels(call, box) == {"x": placed.x, "y": placed.y, "width": placed.width, "height": placed.height}


def test_box_from_pixels_round_trips_box_pixels_on_a_grid(call):
    screens = [[7, 3], [64, 36], [36, 64], [1920, 1080], [1080, 1920]]
    rectangles = [(screen, {"x": x, "y": y, "width": width, "height": height})
                  for screen in screens
                  for x in range(0, screen[0], max(1, screen[0] // 9))
                  for width in {1, 2, screen[0] - x, max(1, (screen[0] - x) // 3)}
                  for y in range(0, screen[1], max(1, screen[1] // 5))
                  for height in {1, screen[1] - y, max(1, (screen[1] - y) // 2)}
                  if x + width <= screen[0] and y + height <= screen[1]]
    boxes = call([["boxFromPixels", [rectangle, screen]] for screen, rectangle in rectangles])
    back = call([["boxPixels", [box, screen]] for box, (screen, _) in zip(boxes, rectangles, strict=True)])
    assert [rectangle for _, rectangle in rectangles] == back
    # Central places each box on the same pixels, so a box the editor saves is drawn where it showed it.
    for box, (screen, rectangle) in zip(boxes, rectangles, strict=True):
        assert place_box(FrameBox(**box), tuple(screen)) == Placement(**rectangle)


def test_box_from_pixels_moves_and_clips_onto_the_screen(call):
    assert _one(call, "boxFromPixels", {"x": -50, "y": 1000, "width": 3000, "height": 0}, SCREEN) == {
        "left": 0, "top": 1000 / 1080, "width": 1, "height": 1 / 1080}
    assert _pixels(call, _one(call, "boxFromPixels", {"x": 1900, "y": -5, "width": 100, "height": 50}, SCREEN)) == {
        "x": 1820, "y": 0, "width": 100, "height": 50}


def test_dragging_the_right_edge_changes_only_the_width(call):
    resized = _one(call, "resizedBy", QUARTER, "right", 10, 0, SCREEN)
    assert _pixels(call, resized) == {"x": 480, "y": 270, "width": 970, "height": 540}
    assert resized["left"] == QUARTER["left"] and resized["top"] == QUARTER["top"]
    assert resized["height"] == QUARTER["height"]
    # An edge moves on its own axis only: the vertical part of a right-edge drag does nothing.
    assert _pixels(call, _one(call, "resizedBy", QUARTER, "right", 10, 25, SCREEN))["height"] == 540


@pytest.mark.parametrize(("handle", "dx", "dy", "expected"), [
    ("left", 10, 0, {"x": 490, "y": 270, "width": 950, "height": 540}),
    ("top", 0, -20, {"x": 480, "y": 250, "width": 960, "height": 560}),
    ("bottom", 0, 30, {"x": 480, "y": 270, "width": 960, "height": 570}),
    ("top-left", -10, -20, {"x": 470, "y": 250, "width": 970, "height": 560}),
    ("top-right", 10, 20, {"x": 480, "y": 290, "width": 970, "height": 520}),
    ("bottom-right", -100, 7, {"x": 480, "y": 270, "width": 860, "height": 547}),
    ("bottom-left", 40, 60, {"x": 520, "y": 270, "width": 920, "height": 600}),
])
def test_each_handle_moves_its_own_edges_and_keeps_the_opposite_ones(call, handle, dx, dy, expected):
    assert _pixels(call, _one(call, "resizedBy", QUARTER, handle, dx, dy, SCREEN)) == expected


@pytest.mark.parametrize(("handle", "dx", "dy", "expected"), [
    ("right", 5000, 0, {"x": 480, "y": 270, "width": 1440, "height": 540}),
    ("left", -5000, 0, {"x": 0, "y": 270, "width": 1440, "height": 540}),
    ("left", 5000, 0, {"x": 1439, "y": 270, "width": 1, "height": 540}),
    ("right", -5000, 0, {"x": 480, "y": 270, "width": 1, "height": 540}),
    ("top", 0, 5000, {"x": 480, "y": 809, "width": 960, "height": 1}),
    ("bottom-right", 9000, 9000, {"x": 480, "y": 270, "width": 1440, "height": 810}),
    ("top-left", -9000, -9000, {"x": 0, "y": 0, "width": 1440, "height": 810}),
    ("bottom-left", 9000, -9000, {"x": 1439, "y": 270, "width": 1, "height": 1}),
])
def test_no_handle_moves_an_edge_past_the_screen_or_below_one_pixel(call, handle, dx, dy, expected):
    assert _pixels(call, _one(call, "resizedBy", QUARTER, handle, dx, dy, SCREEN)) == expected


def test_moving_keeps_the_size_and_stops_at_the_edges(call):
    assert _pixels(call, _one(call, "movedBy", QUARTER, 10, -20, SCREEN)) == {
        "x": 490, "y": 250, "width": 960, "height": 540}
    assert _pixels(call, _one(call, "movedBy", QUARTER, 5000, 5000, SCREEN)) == {
        "x": 960, "y": 540, "width": 960, "height": 540}
    assert _pixels(call, _one(call, "movedBy", QUARTER, -5000, -5000, SCREEN)) == {
        "x": 0, "y": 0, "width": 960, "height": 540}
    whole = {"left": 0, "top": 0, "width": 1, "height": 1}
    assert _one(call, "movedBy", whole, 1, 1, SCREEN) == whole


def test_shape_words_name_the_pixels_and_their_ratio(call):
    words = call([["shapeWords", [{"x": 0, "y": 0, "width": width, "height": height}]] for width, height in [
        (1920, 1080), (800, 800), (1080, 1920), (1440, 1080), (1366, 768), (2560, 1080), (1, 1)]])
    assert words == ["1,920 × 1,080 pixels, 16:9", "800 × 800 pixels, 1:1", "1,080 × 1,920 pixels, 9:16",
                     "1,440 × 1,080 pixels, 4:3", "1,366 × 768 pixels, 16:9", "2,560 × 1,080 pixels, 2.37:1",
                     "1 × 1 pixels, 1:1"]


def test_size_words_name_centimetres_and_the_diagonal_in_inches(call):
    assert _one(call, "sizeWords", [950, 540]) == "95 × 54 cm, 43 in across"
    assert _one(call, "sizeWords", [95, 54]) == "9.5 × 5.4 cm, 4.3 in across"
    assert _one(call, "sizeWords", [475, 237.5]) == "48 × 24 cm, 21 in across"
    assert _one(call, "sizeWords", None) is None
