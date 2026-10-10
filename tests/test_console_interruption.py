"""Output interruption in the console's model (console DDD §15, bead C1): health.js
`interruptionFor` and the Output-interrupted Frame-health state, run under Node as
tests/test_console_flow.py runs the flow kit. The browser half is in
tests/browser/test_operator_health_browser.py and tests/browser/test_player_page_browser.py.
"""

import json
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const health = await import(process.argv[1]);
const facts = await import(process.argv[2]);
const out = {};
const join = await import(process.argv[3]);
const LIVE = { current: { runs: [{ phase: "body", participants: [join.toTarget("lobby")] }] } };
const base = (interruptions, { silent = false, connected = true, runtime = LIVE } = {}) => ({
  readAt: 1000,
  runtime,
  outputInterruptions: interruptions,
  inventory: {
    read_at: 1000, silent_after_seconds: 30, report_interval_seconds: 5,
    players: [{ id: "p-1", device_id: "device-1", authority_epoch: 2, registered_at: 10,
      last_seen: 10, retired_at: null, last_report_at: silent ? 900 : 995 }],
    outputs: [
      { player_id: "p-1", output_id: "HDMI-A-1", observation: { connected } },
      { player_id: "p-1", output_id: "HDMI-A-2", observation: { connected: true } },
    ],
    frames: [
      { id: "lobby", player_id: "p-1", output_id: "HDMI-A-1", generation: 3, calibration_valid: true },
      { id: "hall", player_id: "p-1", output_id: "HDMI-A-2", generation: 1, calibration_valid: true },
      { id: "spare", player_id: null, output_id: null, generation: 0, calibration_valid: false },
    ],
  },
});
const row = { frame_id: "lobby", player_id: "p-1", output_id: "HDMI-A-1", binding_generation: 3,
  cause_layer: "display_host", interrupted_at: 940 };

const served = base([row]);
const found = health.interruptionFor(served, "lobby");
out.fact = { kind: found.fact.kind, text: facts.factText(found.fact), label: found.label };
const lobby = health.frameHealth(served, "lobby");
out.lobby = { state: lobby.state, severity: lobby.severity, cause: lobby.cause, tab: lobby.tab,
  label: lobby.label, tileLabel: lobby.tileLabel };
// Another Frame on the same Player shows nothing: rows are keyed by Frame id only.
out.hall = [health.interruptionFor(served, "hall"), health.frameHealth(served, "hall").state];
// Each producer owner names its layer.
out.layers = ["host_core", "app_manager", "app_effect_broker", "display_host", "player_runtime"]
  .map((cause_layer) => health.interruptionFor(base([{ ...row, cause_layer }]), "lobby").fact.basis);
// No live Run on the Frame: the fact alone, never a claimed Run; an ended Run is not live.
const idle = base([row], { runtime: null });
const ended = base([row], { runtime: { current: { runs: [{ phase: "ended",
  participants: [join.toTarget("lobby")] }] } } });
out.idle = [health.interruptionFor(idle, "lobby").label, health.interruptionFor(idle, "lobby").suffix,
  health.frameHealth(idle, "lobby").tileLabel, health.interruptionFor(ended, "lobby").suffix];
// No row, no served list, or no snapshot: nothing, never "not interrupted".
out.none = [health.interruptionFor(base([]), "lobby"), health.interruptionFor(base(undefined), "lobby"),
  health.interruptionFor(null, "lobby"), health.frameHealth(base([]), "lobby").state];
// Precedence: after "Player silent", before the Panel alarm.
out.silent = health.frameHealth(base([row], { silent: true }), "lobby").state;
out.beforePanel = health.frameHealth(base([row], { connected: false }), "lobby").state;
// Attention lists it as an alarm, with the same label.
const attention = health.wallAttention(served);
out.attention = attention.alarms.map((entry) => [entry.frame.id, entry.health.label]);
// A missing record time drops the age, never prints NaN; a clock step never prints a negative.
out.noTime = facts.factText(health.interruptionFor(base([{ ...row, interrupted_at: null }]), "lobby").fact);
out.future = facts.factText(health.interruptionFor(base([{ ...row, interrupted_at: 1010 }]), "lobby").fact);
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "health.js").as_uri(),
         (SRC / "facts.js").as_uri(), (SRC / "join.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


DISPLAY_HOST_REPORT = "Display Host reported the app surface invalidated or withdrawn"
WORDING = f"Output interrupted (Central's inference: {DISPLAY_HOST_REPORT} · recorded 1 min ago)"


def test_a_served_row_is_a_derived_fact_worded_with_its_layer_and_age():
    out = _run()
    assert out["fact"] == {"kind": "derived", "text": WORDING,
                           "label": WORDING + " · the Run continues"}


def test_a_served_row_makes_its_frame_output_interrupted_an_alarm_and_the_run_continues():
    out = _run()
    assert out["lobby"] == {"state": "output-interrupted", "severity": "alarm", "cause": "output",
                            "tab": "hardware", "label": WORDING + " · the Run continues",
                            "tileLabel": "Output interrupted · the Run continues"}
    assert out["attention"] == [["lobby", WORDING + " · the Run continues"]]


def test_only_the_rows_frame_is_interrupted_and_no_row_shows_nothing():
    out = _run()
    assert out["hall"] == [None, "ok"]
    assert out["none"] == [None, None, None, "ok"]


def test_each_cause_layer_is_worded_with_what_it_actually_reported():
    # contracts/node_protocol.py binds AppProcessFact to the broker and SurfaceFact to
    # Display Host; no other owner can cause a loss, so they get the neutral wording.
    assert _run()["layers"] == [
        "Host Management sent the evidence Central linked to this Output · recorded 1 min ago",
        "App Manager sent the evidence Central linked to this Output · recorded 1 min ago",
        "App Effect Broker reported the app process exited · recorded 1 min ago",
        f"{DISPLAY_HOST_REPORT} · recorded 1 min ago",
        "Player app sent the evidence Central linked to this Output · recorded 1 min ago"]


def test_the_run_continues_only_when_a_live_run_targets_the_frame():
    assert _run()["idle"] == [WORDING, None, "Output interrupted", None]


def test_it_follows_player_silent_and_precedes_the_panel_alarm():
    out = _run()
    assert out["silent"] == "player-silent"
    assert out["beforePanel"] == "output-interrupted"


def test_a_missing_or_later_record_time_never_prints_a_bad_age():
    out = _run()
    assert out["noTime"] == f"Output interrupted (Central's inference: {DISPLAY_HOST_REPORT})"
    assert out["future"].endswith("recorded 0 s ago)")
