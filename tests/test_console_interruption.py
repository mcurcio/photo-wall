"""Output interruption in the console's model (console DDD §15, bead C1): health.js
`interruptionFor`, run under Node as tests/test_console_flow.py runs the flow kit, and what a
Frame's chip says beside it. Since decision 0036 the chip is Central's status, served on Home's
read and worded by domain/frame-status.ts: the console works no status out, so an interruption
row, a silent Pi or a Pi's dark verdict changes a chip only through what Central serves (its
decision table is tests/test_frame_status_policy.py's). The browser half is in
tests/browser/test_operator_health_browser.py and tests/browser/test_player_page_browser.py.
"""

import json
import subprocess
from pathlib import Path

from support.console_status import STATUS_JS
from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = STATUS_JS + r"""
const health = await import(process.argv[1]);
const facts = await import(process.argv[2]);
const out = {};
const join = await import(process.argv[3]);
const status = await import(process.argv[4]);
const LIVE = { current: { runs: [{ phase: "body", participants: [join.toTarget("lobby")] }] } };
const base = (interruptions, { silent = false, connected = true, runtime = LIVE, served = "showing" } = {}) => ({
  readAt: 1000,
  runtime,
  home: served === null ? null : homeOf([frameStatus("lobby", served), frameStatus("hall", "showing")]),
  outputInterruptions: interruptions,
  inventory: {
    read_at: 1000, silent_after_seconds: 30, socket_keepalive_seconds: 10,
    players: [{ id: "p-1", device_id: "device-1", authority_epoch: 2, registered_at: 10,
      last_seen: 10, retired_at: null, last_report_at: silent ? 900 : 995 }],
    outputs: [
      { player_id: "p-1", output_id: "HDMI-A-1", observation: { connected } },
      { player_id: "p-1", output_id: "HDMI-A-2", observation: { connected: true } },
    ],
    frames: [
      { id: "lobby", player_id: "p-1", output_id: "HDMI-A-1", generation: 3, readiness: "ready" },
      { id: "hall", player_id: "p-1", output_id: "HDMI-A-2", generation: 1, readiness: "ready" },
      { id: "spare", player_id: null, output_id: null, generation: 0, readiness: "unbound" },
    ],
  },
});
const row = { frame_id: "lobby", player_id: "p-1", output_id: "HDMI-A-1", binding_generation: 3,
  cause_layer: "display_host", interrupted_at: 940 };

const served = base([row]);
const found = health.interruptionFor(served, "lobby");
out.fact = { kind: found.fact.kind, text: facts.factText(found.fact), label: found.label };
// The Frame's chip is Central's status as served, whatever rows the snapshot holds beside it.
const chipOf = (snapshot, id) => status.frameChip(status.frameStatusIn(snapshot, id));
out.lobby = chipOf(served, "lobby");
// Another Frame on the same Player shows nothing: rows are keyed by Frame id only.
out.hall = [health.interruptionFor(served, "hall"), chipOf(served, "hall").label];
// Each producer owner names its layer.
out.layers = ["host_core", "app_manager", "app_effect_broker", "display_host", "player_runtime"]
  .map((cause_layer) => health.interruptionFor(base([{ ...row, cause_layer }]), "lobby").fact.basis);
// No live Run on the Frame: the fact alone, never a claimed Run; an ended Run is not live.
const idle = base([row], { runtime: null });
const ended = base([row], { runtime: { current: { runs: [{ phase: "ended",
  participants: [join.toTarget("lobby")] }] } } });
out.idle = [health.interruptionFor(idle, "lobby").label, health.interruptionFor(idle, "lobby").suffix,
  health.interruptionFor(ended, "lobby").suffix];
// No row, no served list, or no snapshot: nothing, never "not interrupted".
out.none = [health.interruptionFor(base([]), "lobby"), health.interruptionFor(base(undefined), "lobby"),
  health.interruptionFor(null, "lobby")];
// The console judges no precedence: a silent Player's record, an unplugged Panel or a row beside
// it leaves the chip at Central's word, and a status Central has not served is Can't tell.
out.served = [chipOf(base([row], { silent: true }), "lobby"), chipOf(base([row], { connected: false }), "lobby"),
  chipOf(base([row], { served: "not-showing" }), "lobby"), chipOf(base([], { served: null }), "lobby"),
  chipOf(null, "lobby")];
// A missing record time drops the age, never prints NaN; a clock step never prints a negative.
out.noTime = facts.factText(health.interruptionFor(base([{ ...row, interrupted_at: null }]), "lobby").fact);
out.future = facts.factText(health.interruptionFor(base([{ ...row, interrupted_at: 1010 }]), "lobby").fact);
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "health.js").as_uri(),
         (SRC / "facts.js").as_uri(), (SRC / "join.js").as_uri(), (SRC / "domain/frame-status.ts").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


DISPLAY_HOST_REPORT = "Display Host reported the app surface invalidated or withdrawn"
WORDING = f"Output interrupted (Central's inference: {DISPLAY_HOST_REPORT} · recorded 1 min ago)"


def test_a_served_row_is_a_derived_fact_worded_with_its_layer_and_age():
    out = _run()
    assert out["fact"] == {"kind": "derived", "text": WORDING,
                           "label": WORDING + " · the Run continues"}


def test_a_served_row_does_not_change_its_frames_chip_which_is_centrals_status():
    """Decision 0036: an Output interruption has no row in Central's decision table, so the row is
    the Pi page's fact and the Frame's chip stays what Central served."""
    out = _run()
    assert out["lobby"] == {"severity": "ok", "label": "Showing"}


def test_only_the_rows_frame_is_interrupted_and_no_row_shows_nothing():
    out = _run()
    assert out["hall"] == [None, "Showing"]
    assert out["none"] == [None, None, None]


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
    assert _run()["idle"] == [WORDING, None, None]


def test_the_console_judges_no_precedence_the_chip_is_what_central_served_or_cant_tell():
    """The order of causes (a silent Pi, a dark verdict, the display) is Central's decision table,
    held by tests/test_frame_status_policy.py. Here: nothing in the snapshot beside Home's document
    moves the chip, and a status that was not served is Can't tell, never a guess."""
    showing, cant_tell = {"severity": "ok", "label": "Showing"}, {"severity": "unknown", "label": "Can't tell"}
    assert _run()["served"] == [showing, showing, {"severity": "alarm", "label": "Not showing"}, cant_tell, cant_tell]


def test_a_missing_or_later_record_time_never_prints_a_bad_age():
    out = _run()
    assert out["noTime"] == f"Output interrupted (Central's inference: {DISPLAY_HOST_REPORT})"
    assert out["future"].endswith("recorded 0 s ago)")


DARK_SCRIPT = STATUS_JS + r"""
const status = await import(process.argv[1]);
const time = await import(process.argv[2]);
time.setHouseClock({ zone: "Europe/London", clock: "24h" });
// 2026-10-20T07:10:37Z: Central's time of the change; the Pi's own time rides as evidence.
const SINCE = Date.UTC(2026, 9, 20, 7, 10, 37) / 1000;
const NOW = SINCE + 600;
const problem = frameProblem("hearth", "photo app", "app-descriptor-limit", { since: SINCE,
  fix: { kind: "restart-app", device_id: "device-1" }, detail: "app_descriptor_limit", pi_time: "2026-10-20T07:10:37Z" });
const dark = frameStatus("hearth", "not-showing", { row: "dark", since: SINCE, problem, can: ["reboot-pi", "restart-app"] });
const said = { pi: "Pi e-1", now: NOW };
const view = status.problemView(problem, dark, "Hearth", said);
const out = {
  chip: status.frameChip(dark),
  line: status.statusLine(dark, said),
  tab: status.tabForStatus(dark, "overview"),
  view: { subject: view.subject, label: view.verdict.label, since: view.since, what: view.what, fix: view.fix.label,
    details: Object.fromEntries(view.details) },
  // The same fault while the display is off on purpose: Needs a look, and when it will matter (finding F1).
  night: status.statusLine(frameStatus("hearth", "needs-a-look", { row: "wont-show", since: SINCE, problem,
    power: "night-off", until: SINCE + 3600 }), said),
  setup: status.frameChip(frameStatus("hearth", null, { setup: "needs-a-pi" })),
  lit: status.frameChip(frameStatus("hearth", "showing")),
  resting: status.tileLine({ status: frameStatus("hearth", "resting", { row: "nothing-wanted" }), playing: null }, said),
};
time.setHouseClock({ zone: "Europe/London", clock: "12h" });
out.twelve = status.statusLine(dark, said);
console.log(JSON.stringify(out));
"""


def _dark():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", DARK_SCRIPT, "--", (SRC / "domain/frame-status.ts").as_uri(),
         (SRC / "timeWords.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


DARK_LINE = "Not showing since 08:10 · The photo app ran out of open files."


def test_a_dark_frame_is_an_alarm_worded_by_its_part_with_centrals_time_on_the_house_clock():
    # Issue #126, as decision 0036 words it: the Pi's verdict `app_descriptor_limit` is Central's
    # problem `app-descriptor-limit`, part photo app, fix Restart app. 07:10:37 UTC is 08:10 in
    # London (BST), to the minute; the Pi's own time and the code are under Details.
    out = _dark()
    assert out["chip"] == {"severity": "alarm", "label": "Not showing"}
    assert (out["line"], out["tab"]) == (DARK_LINE, "hardware")
    assert out["twelve"] == "Not showing since 8:10 am · The photo app ran out of open files."
    view = out["view"]
    assert (view["subject"], view["label"], view["since"], view["what"], view["fix"]) == (
        "Hearth", "Not showing", "08:10", "The photo app ran out of open files.", "Restart app")
    assert view["details"]["Failing part"] == "the photo app"
    assert view["details"]["Code"] == "app-descriptor-limit (app_descriptor_limit)"
    assert view["details"]["On the Pi's own clock"] == "2026-10-20T07:10:37Z"


def test_the_same_fault_under_night_off_says_when_it_will_matter_and_setup_is_not_status():
    out = _dark()
    assert out["night"] == "Needs a look since 08:10 · The photo app ran out of open files. Won't show at 09:10."
    assert out["setup"] == {"severity": "todo", "label": "Needs a Pi"}
    assert out["lit"] == {"severity": "ok", "label": "Showing"}
    assert out["resting"] == "Nothing to show right now"      # dark on purpose: nothing is to play
