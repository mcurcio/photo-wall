"""Pure reference preview used by the Frame deletion confirmation."""

import json
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"


def test_frame_stored_references_walk_inline_scenes_and_the_programs_playing_them():
    _require_node()
    script = r"""
const { frameStoredReferences } = await import(process.argv[1]);
const snapshot = { runtime: {
  current: { now: 1000 },
  definitions: {
    "z-outro": { outro_contributions: [{ target: "frame:wall" }] },
    "a-inline": { children: [{ scene: { children: [{ scene: {
      contributions: [{ target: "frame:wall" }] } }] } }] },
    "other": { contributions: [{ target: "frame:other" }] },
  },
  programs: {
    "z-next": { scene_id: "z-outro", name: "Z" },
    "a-next": { scene_id: "a-inline", name: "A" },
    "other": { scene_id: "other", name: "Other" },
  },
} };
console.log(JSON.stringify({
  wall: frameStoredReferences(snapshot, "wall"),
  none: frameStoredReferences(snapshot, "missing"),
  empty: frameStoredReferences(null, "wall"),
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", (SRC / "sceneTargets.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert json.loads(result.stdout) == {
        "wall": {"scenes": ["a-inline", "z-outro"], "programs": ["a-next", "z-next"]},
        "none": {"scenes": [], "programs": []},
        "empty": {"scenes": [], "programs": []},
    }
