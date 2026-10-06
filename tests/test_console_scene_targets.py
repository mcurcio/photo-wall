"""Pure Scene content projection shared by Show and Scene cards."""

import json
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"


def test_scene_content_summary_walks_every_phase_and_deduplicates():
    _require_node()
    script = r"""
const targets = await import(process.argv[1]);
const nested = {
  contributions: [{ target: "frame:b", source_refs: ["holiday:1"] }],
  outro_contributions: [{ target: "frame:a", asset_refs: ["still-1", "still-2"] }],
  children: [{ scene: {
    contributions: [{ target: "frame:b", source_refs: ["holiday:1", "family:2"] }],
    children: [{ scene: { outro_contributions: [
      { target: "frame:c", asset_refs: ["still-1"] },
      { target: "actuator:light", kind: "actuator" },
    ] } }],
  } }],
};
console.log(JSON.stringify({
  summary: targets.sceneContentSummary(nested),
  frames: targets.sceneFrames(nested),
  sources: targets.sceneSourceRefs(nested),
  authored: targets.sceneHasAuthoredMedia(nested),
  empty: targets.sceneContentSummary(undefined),
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", (SRC / "sceneTargets.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert json.loads(result.stdout) == {
        "summary": {
            "frames": ["a", "b", "c"],
            "sourceRefs": ["family:2", "holiday:1"],
            "assetRefs": ["still-1", "still-2"],
        },
        "frames": ["a", "b", "c"],
        "sources": ["family:2", "holiday:1"],
        "authored": True,
        "empty": {"frames": [], "sourceRefs": [], "assetRefs": []},
    }
