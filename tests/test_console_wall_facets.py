"""The Wall facets' pure parts (console DDD §19-§21; bead D1): players.js `identifyOffer` and
`enrolledFact`, health.js's Calibration and Panel-at-enrollment words, run under Node as
tests/test_console_players.py runs them, plus the source scans for the retired words and
modules. The browser half is tests/browser/test_console_frame_page_browser.py (the Frame
page's Position, Picture and Frame profile),
test_operator_binding_browser.py (Hardware, Identify) and test_player_page_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const health = await import(process.argv[1]);
const players = await import(process.argv[2]);
const { factText } = await import(process.argv[3]);
const out = {};
const player = (id, extra = {}) => ({ id, device_id: `device-${id}`, authority_epoch: 4,
  registered_at: 10, last_seen: 940, retired_at: null, last_report_at: 995, ...extra });
const snapshot = { inventory: {
  read_at: 1000, silent_after_seconds: 30, report_interval_seconds: 5,
  players: [player("p-bound"), player("p-retired", { retired_at: 900 })],
  outputs: [
    { player_id: "p-bound", output_id: "HDMI-A-1", observation: { connected: true } },
    { player_id: "p-bound", output_id: "HDMI-A-2", observation: { connected: true } },
    { player_id: "p-bound", output_id: "HDMI-A-3", observation: { connected: false } },
    { player_id: "p-retired", output_id: "HDMI-A-1", observation: { connected: true } },
  ],
  frames: [
    { id: "lobby", player_id: "p-bound", output_id: "HDMI-A-1", calibration_valid: false },
    { id: "spare", player_id: null, output_id: null, calibration_valid: false },
  ],
} };
// --- identifyOffer: one rule for the Player page and the Binding facet's picker.
out.identify = [
  players.identifyOffer(snapshot, "p-bound", "HDMI-A-2"),  // a Bound Player's free second Output
  players.identifyOffer(snapshot, "p-bound", "HDMI-A-1"),  // bound to a Frame
  players.identifyOffer(snapshot, "p-bound", "HDMI-A-3"),  // no Panel listed at enrollment
  players.identifyOffer(snapshot, "p-retired", "HDMI-A-1"),  // Player retired
  players.identifyOffer(snapshot, "p-bound", "HDMI-A-9"),  // not listed
  players.identifyOffer(snapshot, "nobody", "HDMI-A-1"),
];
// --- enrolledFact: Central's enrollment record, a `set` fact with Central's age.
const enrolled = players.enrolledFact(player("p-bound"), 1000);
out.enrolled = [
  enrolled.kind, factText(enrolled),
  factText(players.enrolledFact(player("p-bound", { last_seen: null }), 1000)),
  factText(players.enrolledFact(player("p-bound"), null)),
  factText(players.enrolledFact(null, 1000)),
];
// --- Frame health: needs-calibration, and the Panel alarm in the one enrollment wording.
const calibrate = health.frameHealth(snapshot, "lobby");
const unplugged = health.frameHealth({ inventory: { ...snapshot.inventory,
  outputs: snapshot.inventory.outputs.map((output) => output.output_id === "HDMI-A-1"
    && output.player_id === "p-bound" ? { ...output, observation: { connected: false } } : output),
} }, "lobby");
const pick = ({ state, severity, cause, label, tileLabel, tab }) =>
  ({ state, severity, cause, label, tileLabel, tab });
out.health = [pick(calibrate), pick(unplugged)];
out.panelWording = factText(players.panelAtEnrollment({ connected: false }, null, null));
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "health.js").as_uri(),
         (SRC / "players.js").as_uri(), (SRC / "facts.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_identify_is_offered_on_any_unbound_connected_output_of_an_active_player():
    assert _run()["identify"] == [
        {"offer": True},
        {"offer": False, "reason": "Central identifies only unbound Outputs"},
        {"offer": False, "reason": "Connect a Panel and restart the Player app"},
        {"absent": True},
        {"absent": True},
        {"absent": True},
    ]


def test_the_enrolled_fact_is_centrals_record_with_centrals_age():
    assert _run()["enrolled"] == [
        "set",
        "Player app enrolled 1 min ago (authority epoch 4)",
        "Unknown: Central's enrollment record time is not served",
        "Unknown: Central's enrollment record time is not served",
        "Unknown: the Player app has not enrolled",
    ]


def test_frame_health_says_calibration_and_keeps_the_panel_alarm_worded_as_a_record():
    out = _run()
    assert out["health"] == [
        {"state": "needs-calibration", "severity": "todo", "cause": "calibration",
         "label": "Needs calibration", "tileLabel": "Needs calibration", "tab": "position"},
        # An unplugged-at-enrollment Panel on a bound Frame is still an alarm (§19), in the
        # one wording panelAtEnrollment renders, and opens Hardware, where that record is shown.
        {"state": "no-panel-at-enrollment", "severity": "alarm", "cause": "panel",
         "label": "No Panel listed as connected at the Player app's last enrollment (may be stale)",
         "tileLabel": "No Panel listed at the last enrollment", "tab": "hardware"},
    ]
    assert out["health"][1]["label"] == out["panelWording"]


_RETIRED_WORDS = re.compile(r"Commission|commission|Trial\b|\bT1\b|\bT2\b")


def test_no_console_string_says_commissioning_trial_or_a_tier():
    offenders = []
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx"), SRC / "routeSamples.json"]:
        for number, line in enumerate(module.read_text().splitlines(), 1):
            if _RETIRED_WORDS.search(line):
                offenders.append(f"{module.name}:{number}: {line.strip()}")
    assert offenders == []


def test_the_gated_seam_and_the_recovery_banner_are_deleted():
    for name in ("capability.js", "GatedArea.jsx", "recovery.js", "Commissioning.jsx"):
        assert not (SRC / name).exists(), name
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]:
        text = module.read_text()
        for phrase in ("capability.js", "GatedArea", "recovery.js", "useRecovery", "Recovered",
                       "not yet available"):
            assert phrase not in text, (module.name, phrase)
