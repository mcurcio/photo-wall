"""The Wall's daily face and Edit layout (console DDD §61, G1-G3; bead W1).

Static: the read-only Plan, the select-only Unplaced tray and the To finish list reach no
write module, by the R4 test's own fail-closed import scan, followed transitively. Every Plan
and tray write lives in LayoutEditor.jsx (Edit layout, `#/wall/layout`), which is the
positive control.

Model: unfinished.js `wallUnfinished` (G2: structural, from `set` records only) and Central's
problems on Home (evidence only; decision 0036) never share an item, and domain/frame-status.ts
`tabForStatus` opens the cause tab or the Frame page's Overview fallback. Run under Node as the
other console model tests are.

The browser half is tests/browser/test_operator_wall_browser.py.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from support.console_status import STATUS_JS
from test_console_flow import _require_node
from test_console_routes_r4 import WRITE_MODULES, scan_closure

SRC = Path(__file__).parents[1] / "central/console/src"

# The daily face's select-only modules, and G1's list model and component.
READ_ONLY = ["Plan.jsx", "UnplacedTray.jsx", "unfinished.js", "WallUnfinished.jsx"]


def _closure(path, root=SRC.parent):
    """Every console module `path` reaches by imports (the R4 scan, which fails closed)."""
    return scan_closure(path, root)


@pytest.mark.parametrize("name", READ_ONLY)
def test_the_daily_face_modules_reach_no_write_module(name):
    reached = _closure(SRC / name)
    assert name in reached
    assert not reached & WRITE_MODULES, sorted(reached & WRITE_MODULES)


def test_edit_layout_owns_the_plan_and_tray_writes():
    # Positive control: the writes the daily face lost are reached from Edit layout.
    reached = _closure(SRC / "LayoutEditor.jsx")
    assert {"framesApi.js", "ConfirmAction.jsx", "useMutate.js", "Plan.jsx",
            "UnplacedTray.jsx"} <= reached


def test_the_scan_catches_a_write_import_added_to_a_read_only_module(tmp_path):
    # A mutation of the guarded files, in a copy: one added write import fails the check.
    # The whole tree: Plan.jsx reads a Frame's status through domain/frame-status.ts.
    src = tmp_path / "src"
    shutil.copytree(SRC, src)
    plan = src / "Plan.jsx"
    plan.write_text('import { placeFrame } from "./framesApi.js";\n' + plan.read_text())
    assert "framesApi.js" in _closure(plan, tmp_path)


SCRIPT = STATUS_JS + r"""
const status = await import(process.argv[1]);
const { wallUnfinished } = await import(process.argv[2]);
const player = (id, extra = {}) => ({ id, device_id: `device-${id}`, authority_epoch: 1,
  registered_at: 10, last_seen: 900, retired_at: null, last_report_at: 995, ...extra });
const frame = (id, extra = {}) => ({ id, surface_id: "wall", x_mm: 10, y_mm: 10,
  size_mm: null, player_id: null, output_id: null, readiness: "unbound",
  ...extra });
const output = (player_id, output_id) => ({ player_id, output_id,
  observation: { connected: true } });
// Central's status of each Frame, as Home's read serves it: the silent Pi's Frame has a problem
// naming its Pi; the Frames still in setup have a step and no word.
const silent = frameStatus("dark", "cant-tell", { row: "pi-not-heard",
  problem: frameProblem("dark", "pi", "pi-not-heard", { layer: "presence" }) });
const statuses = [frameStatus("unbound", null, { setup: "needs-a-pi" }), silent, frameStatus("ok", "showing"),
  frameStatus("raw", null, { setup: "check-position" }), frameStatus("moved", null, { setup: "display-changed" }),
  frameStatus("origin", null, { setup: "needs-a-pi" })];
const snapshot = { home: homeOf(statuses, [silent.problem]), inventory: {
  read_at: 1000, silent_after_seconds: 30, socket_keepalive_seconds: 10,
  players: [player("heard"), player("silent", { last_report_at: 100 }), player("raw")],
  outputs: [output("heard", "HDMI-A-1"), output("heard", "HDMI-A-2"), output("silent", "HDMI-A-1"),
    output("raw", "HDMI-A-1")],
  frames: [
    frame("unbound"),
    frame("dark", { player_id: "silent", output_id: "HDMI-A-1", readiness: "ready" }),
    frame("ok", { player_id: "heard", output_id: "HDMI-A-1", readiness: "ready" }),
    frame("raw", { player_id: "raw", output_id: "HDMI-A-1", readiness: "position-needed" }),
    frame("moved", { player_id: "heard", output_id: "HDMI-A-2", readiness: "display-changed" }),
    frame("origin", { x_mm: 0, y_mm: 0 }),
  ],
} };
console.log(JSON.stringify({
  unfinished: wallUnfinished(snapshot),
  problems: snapshot.home.house.problems.map((problem) => problem.frame_id),
  tab: ["unbound", "ok", "dark", "raw", "moved", "never-served"].map(
    (id) => status.tabForStatus(status.frameStatusIn(snapshot, id), "overview")),
  empty: wallUnfinished({ inventory: { frames: [] } }),
}));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "domain/frame-status.ts").as_uri(),
         (SRC / "unfinished.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_to_finish_is_structural_and_homes_problems_are_evidence():
    out = _run()
    wall = lambda **extra: {"section": "wall", **extra}  # noqa: E731
    # Frame order; each Frame's missing `set` records in bring-up order. An unbound Frame
    # needs a Player, not a calibration; an origin Frame is also not on the plan.
    assert out["unfinished"] == [
        # Another display on its port: the Hardware tab's card, not Position (1b K1).
        {"frameId": "moved", "step": "display", "route": wall(id="moved", tab="hardware")},
        {"frameId": "origin", "step": "place", "route": wall(mode="layout")},
        {"frameId": "origin", "step": "bind", "route": wall(id="origin", tab="hardware")},
        {"frameId": "raw", "step": "calibrate", "route": wall(id="raw", tab="position")},
        {"frameId": "unbound", "step": "bind", "route": wall(id="unbound", tab="hardware")},
    ]
    # The silent bound Pi's Frame is one of Central's problems on Home and not a To finish item;
    # a Frame still in setup is a To finish item and has no problem (setup is not status).
    assert out["problems"] == ["dark"]
    assert not set(out["problems"]) & {item["frameId"] for item in out["unfinished"]}
    assert out["empty"] == []


def test_tab_for_opens_the_cause_or_overview():
    # Needs a Pi -> Hardware; Showing -> the Frame page's Overview fallback; a problem naming
    # the Pi -> Hardware; check its Position -> Position; display changed -> Hardware; a status
    # Central has not served -> the fallback.
    assert _run()["tab"] == ["hardware", "overview", "hardware", "position", "hardware", "overview"]
