"""The Wall's daily face and Edit layout (console DDD §61, G1-G3; bead W1).

Static: the read-only Plan, the select-only Unplaced tray and the To finish list reach no
write module, by the R4 test's own fail-closed import scan, followed transitively. Every Plan
and tray write lives in LayoutEditor.jsx (Edit layout, `#/wall/layout`), which is the
positive control.

Model: unfinished.js `wallUnfinished` (G2: structural, from `set` records only) and health.js
`wallAttention` (evidence only) never share an item, and health.js `facetFor` opens the cause
facet or the Wall's Status fallback. Run under Node as the other console model tests are.

The browser half is tests/browser/test_operator_wall_browser.py.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_console_flow import _require_node
from tests.test_console_routes_r4 import WRITE_MODULES, scan_closure

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
    src = tmp_path / "src"
    src.mkdir()
    for module in SRC.iterdir():
        if module.is_file():
            (src / module.name).write_bytes(module.read_bytes())
    plan = src / "Plan.jsx"
    plan.write_text('import { moveFrame } from "./framesApi.js";\n' + plan.read_text())
    assert "framesApi.js" in _closure(plan, tmp_path)


SCRIPT = r"""
const health = await import(process.argv[1]);
const { wallUnfinished } = await import(process.argv[2]);
const player = (id, extra = {}) => ({ id, device_id: `device-${id}`, authority_epoch: 1,
  registered_at: 10, last_seen: 900, retired_at: null, last_report_at: 995, ...extra });
const frame = (id, extra = {}) => ({ id, surface_id: "wall", x_mm: 10, y_mm: 10,
  width_mm: 300, height_mm: 500, player_id: null, output_id: null, calibration_valid: false,
  ...extra });
const output = (player_id, output_id) => ({ player_id, output_id,
  observation: { connected: true } });
const snapshot = { inventory: {
  read_at: 1000, silent_after_seconds: 30, report_interval_seconds: 5,
  players: [player("heard"), player("silent", { last_report_at: 100 }), player("raw")],
  outputs: [output("heard", "HDMI-A-1"), output("silent", "HDMI-A-1"), output("raw", "HDMI-A-1")],
  frames: [
    frame("unbound"),
    frame("dark", { player_id: "silent", output_id: "HDMI-A-1", calibration_valid: true }),
    frame("ok", { player_id: "heard", output_id: "HDMI-A-1", calibration_valid: true }),
    frame("raw", { player_id: "raw", output_id: "HDMI-A-1" }),
    frame("origin", { x_mm: 0, y_mm: 0 }),
  ],
} };
const attention = health.wallAttention(snapshot);
console.log(JSON.stringify({
  unfinished: wallUnfinished(snapshot),
  attention: { ...attention, alarms: attention.alarms.map((entry) => entry.frame.id) },
  facet: ["unbound", "ok", "dark", "raw"].map(
    (id) => health.facetFor(health.frameHealth(snapshot, id), "status")),
  empty: wallUnfinished({ inventory: { frames: [] } }),
}));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "health.js").as_uri(),
         (SRC / "unfinished.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_to_finish_is_structural_and_attention_is_evidence():
    out = _run()
    wall = lambda **extra: {"section": "wall", **extra}  # noqa: E731
    # Frame order; each Frame's missing `set` records in bring-up order. An unbound Frame
    # needs a Player, not a calibration; an origin Frame is also not on the plan.
    assert out["unfinished"] == [
        {"frameId": "origin", "step": "place", "route": wall(mode="layout")},
        {"frameId": "origin", "step": "bind", "route": wall(id="origin", facet="binding")},
        {"frameId": "raw", "step": "calibrate", "route": wall(id="raw", facet="calibration")},
        {"frameId": "unbound", "step": "bind", "route": wall(id="unbound", facet="binding")},
    ]
    # The silent bound Player's Frame is an attention row and not a To finish item; the
    # unbound Frame is a To finish item and not an attention row. No `todos` remain.
    assert out["attention"] == {"frameCount": 5, "awaiting": 0, "alarms": ["dark"]}
    assert "dark" not in {item["frameId"] for item in out["unfinished"]}
    assert out["empty"] == []


def test_facet_for_opens_the_cause_or_status():
    # Unbound -> Binding; ok -> the Wall's Status fallback; a silent Player -> Binding;
    # needs calibration -> Calibration.
    assert _run()["facet"] == ["binding", "status", "binding", "calibration"]
