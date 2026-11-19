"""The fleet views' pure parts (console DDD §5 rule 2, §9, §11; bead B1): facts.js (`fact`,
`factText`), players.js (`playersByDevice`) and nodeRead.js's model (`layerEvidence`,
`hostStart`), run under Node as tests/test_console_flow.py runs the
flow kit. Without Node it skips on a developer machine, but FAILS where the checks are meant
to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set). The browser half is
tests/browser/test_player_page_browser.py.
"""

import json
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const facts = await import(process.argv[1]);
const players = await import(process.argv[2]);
const node = await import(process.argv[3]);
const { fact, factText } = facts;
const out = {};
const text = (spec) => factText(fact(spec));

// --- fact(): one wording per kind; a missing label becomes unknown, naming it.
out.wording = [
  text({ kind: "set", value: "Bound to Frame lobby-left" }),
  text({ kind: "set", value: "Issued", receivedAt: 100, readAt: 160 }),
  text({ kind: "reported", source: "Host Management", receipt: "latest", receivedAt: 100, readAt: 104 }),
  text({ kind: "reported", source: "App Manager", receipt: "latest", value: "preparation ready",
         receivedAt: 100, readAt: 100 }),
  text({ kind: "reported", source: "App Effect Broker", receipt: "first", value: "the app running",
         receivedAt: 0, readAt: 3 * 86400 }),
  text({ kind: "claimed", value: "Serial 10000000c0ffee42", source: "the box" }),
  text({ kind: "claimed", value: "Boot b-1", source: "the box", receipt: "first", receivedAt: 10, readAt: 70 }),
  text({ kind: "claimed", value: "app abc running", source: "its serial check-in", receipt: "latest",
         receivedAt: 67, readAt: 70 }),
  text({ kind: "derived", value: "Interrupted", basis: "a later boot was admitted" }),
  text({ kind: "unknown", why: "no layer observes them" }),
];
out.missing = [
  text({ kind: "reported", source: "Host Management", receipt: "latest", readAt: 5,
         field: "host_observation.received_at" }),
  text({ kind: "reported", source: "Host Management", receipt: "latest", receivedAt: null, readAt: 5 }),
  text({ kind: "reported", source: "Host Management", receipt: "latest", receivedAt: 1 }),
  text({ kind: "reported", source: "Host Management", receivedAt: 1, readAt: 5 }),
  text({ kind: "reported", receipt: "latest", receivedAt: 1, readAt: 5 }),
  text({ kind: "reported", source: "App Effect Broker", receipt: "first", receivedAt: 1, readAt: 5 }),
  text({ kind: "claimed", value: "Serial x" }),
  text({ kind: "claimed", value: "Boot b-1", source: "the box", receivedAt: 10, readAt: 70 }),
  text({ kind: "derived", value: "Interrupted" }),
  text({ kind: "set", value: "" }),
  text({ kind: "bogus", value: "x" }),
  text({ kind: "unknown" }),
];
// Never throws, never builds an unlabelled fact, and the value is frozen.
const odd = [undefined, null, 7, "x", { kind: "reported", receivedAt: "1", readAt: {} }];
out.neverThrows = odd.map((spec) => fact(spec).kind);
const built = fact({ kind: "set", value: "x" });
out.frozen = Object.isFrozen(built);
out.notAFact = [factText(null), factText({ kind: "nope" })];

// --- playersByDevice: one row per box, enrolled first, then boxes seen only at boot.
const snapshot = { inventory: {
  read_at: 1000, silent_after_seconds: 30, socket_keepalive_seconds: 10,
  players: [
    { id: "p-b", device_id: "device-b", authority_epoch: 1, registered_at: 20, last_seen: 20,
      retired_at: null, last_report_at: 990 },
    { id: "p-a", device_id: "device-a", authority_epoch: 3, registered_at: 10, last_seen: 400,
      retired_at: null, last_report_at: null },
    { id: "p-r", device_id: "device-r", authority_epoch: 1, registered_at: 30, last_seen: 30,
      retired_at: 900 },
  ],
  outputs: [
    { player_id: "p-b", output_id: "HDMI-A-2", observation: { connected: true } },
    { player_id: "p-b", output_id: "HDMI-A-1", observation: { connected: true } },
    { player_id: "p-a", output_id: "HDMI-A-1", observation: { connected: true } },
  ],
  frames: [
    { id: "lobby", player_id: "p-b", output_id: "HDMI-A-1", generation: 1 },
    { id: "hall", player_id: null, output_id: null, generation: 0 },
  ],
} };
const bootFacts = { loaded: true, unavailable: false, devices: new Map([
  ["device-a", { device_id: "device-a", serial: "10000000c0ffee42" }],
  ["device-z", { device_id: "device-z", serial: "10000000c0ffee99" }],
  ["device-y", { device_id: "device-y", serial: null }],
]) };
out.rows = players.playersByDevice(snapshot, bootFacts).map((row) => ({
  deviceId: row.deviceId, player: row.player?.id ?? null, standing: row.standing,
  standingLabel: row.standingLabel, name: row.name, frames: row.frames,
}));
out.noBootFacts = players.playersByDevice(snapshot, null).map((row) => row.deviceId);
out.empty = players.playersByDevice(null, null);

// --- layerEvidence: three rows, each fact labelled; the host's from its line (decision 0034).
const read = { read_at: 2000, boot_claims: [
  { kernel_boot_id: "boot-2", first_received_at: 1400, offer_refusal: null },
  { kernel_boot_id: "boot-1", first_received_at: 100, offer_refusal: null },
], host: { epoch: "start-2", born_at: 1400, received_at: 1996 } };
const reporting = { read, readAt: 2000, error: null };
const rows = (nodeDevice, playerId = "p-a") =>
  node.layerEvidence({ nodeDevice, snapshot, playerId }).map((row) => ({
    key: row.key, layer: row.layer, level: row.level,
    facts: row.facts.map((entry) => [entry.label, factText(entry.fact)]),
    details: row.details,
  }));
out.layers = rows(reporting);
out.silentApp = rows(reporting, "p-b").map((row) => row.facts[0][1]);
out.noHostLine = rows({ ...reporting, read: { ...read, host: null } }).map((row) => row.facts[0][1]);
out.noHostValue = rows({ ...reporting, read: { ...read, host: { ...read.host, received_at: null } } })
  .map((row) => row.facts[0][1]);
// An unreachable read mid-session is an ordinary failed read (Part E §25).
out.disabled = rows({ read: null, readAt: null,
                      error: { code: "unanswered", status: null } }).map((row) => row.facts[0][1]);
out.noRecord = rows({ read: null, readAt: null,
                      error: { code: "node_device_unavailable", status: 403 } }).map((row) => row.facts[0][1]);
out.notYet = rows({ read: null, readAt: null, error: null }).map((row) => row.facts[0][1]);
out.notEnrolled = rows(reporting, null)[2].facts;
// --- Display Host (C2): the display read's newest exchange per Output on the current boot.
const displayOutputs = [
  { output_id: "HDMI-A-1", received_at: 1997, connected: true,
    surface: { frame_id: "lobby", binding_generation: 3, config_revision: 7 },
    receipt: { matches_surface: true, age_ms: 2400 } },
  { output_id: "HDMI-A-2", received_at: 1990, connected: false, surface: null,
    receipt: { matches_surface: false, age_ms: 100 } },
  { output_id: "HDMI-A-3", received_at: 1985, connected: true,
    surface: { frame_id: "hall", binding_generation: 1, config_revision: 2 }, receipt: null },
];
const withDisplay = (display_outputs) => ({ ...reporting, read: { ...read, display_outputs } });
out.display = rows(withDisplay(displayOutputs))[1];
out.displayModel = node.displayOutputs(withDisplay(displayOutputs), 2000).map((output) => ({
  outputId: output.outputId, facts: output.facts.map(factText) }));
out.displayEmpty = rows(withDisplay([]))[1].facts;
// An Output Central could not decode (served undecodable): one Unknown fact, the rest unaffected.
out.displayUndecodable = rows(withDisplay([displayOutputs[1],
  { output_id: "HDMI-A-9", received_at: 1999, undecodable: true }]))[1].facts;
out.displayNoTime = node.displayOutputs(withDisplay([{ ...displayOutputs[0], received_at: null }]), 2000)
  .map((output) => output.facts.map(factText));
// Central reads no reports from a retired Player: its null last_report_at is not "no report".
out.retiredApp = rows(reporting, "p-r")[2].facts;
// Payload drift (a field of the wrong type) throws, for the section's boundary to contain.
out.drift = (() => {
  try { node.layerEvidence({ nodeDevice: { ...reporting, read: { ...read, display_outputs: [null] } }, snapshot,
                             playerId: "p-a" }); return "rendered"; }
  catch { return "threw"; }
})();

// --- hostStart: the host line's start as Central keeps it, never linked to a request.
const start = (nodeDevice) => {
  const result = node.hostStart(nodeDevice);
  return { epoch: result.epoch ?? null, text: factText(result.fact ?? result.none) };
};
out.start = [
  start(reporting),
  start({ ...reporting, read: { ...read, host: null } }),
  start({ read: null, error: { code: "unanswered", status: null } }),
];
// --- panelAtEnrollment: one wording; connected=false is Central's own record, not a report.
out.panel = [
  factText(players.panelAtEnrollment({ connected: true, width_px: 1920 }, 160, 100)),
  factText(players.panelAtEnrollment({ connected: false }, 160, 100)),
  players.panelAtEnrollment({ connected: false }, 160, 100).kind,
  factText(players.panelAtEnrollment(null, 160, 100)),
];
// --- busLinkFact: the Node API link as Central's inference, or unknown; never throws.
const link = (busLink, readAt) => {
  const value = facts.busLinkFact(busLink, readAt);
  return [value.kind, factText(value)];
};
out.busLink = [
  link({ linked: true, changed_at: 100, hub: "reachable" }, 300),
  link({ linked: false, changed_at: 280, hub: "reachable" }, 300),
  link({ linked: true, changed_at: 100, hub: "unreachable" }, 300),
  link({ linked: true, changed_at: 100, hub: "worker_stopped" }, 300),
  link({ linked: null, changed_at: null, hub: "no_hub" }, 300),
  link({ linked: null, changed_at: null, hub: "reachable" }, 300),
  link(undefined, 300),
  link({ linked: true, changed_at: 100, hub: "reachable" }, null),
  link({ linked: "yes", changed_at: "x", hub: {} }, 300),
];
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "facts.js").as_uri(),
         (SRC / "players.js").as_uri(), (SRC / "nodeRead.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_each_truth_kind_has_its_one_wording():
    out = _run()
    assert out["wording"] == [
        "Bound to Frame lobby-left",
        "Issued · recorded 1 min ago",
        "Host Management last reported 4 s ago",
        "App Manager last reported 0 s ago · preparation ready",
        "App Effect Broker reported the app running · first received 3 d ago",
        "Serial 10000000c0ffee42 (claimed at boot by the box, unverified)",
        "Boot b-1 (claimed at boot by the box, unverified) · first received 1 min ago",
        # A claim repeated on every check-in carries its LATEST receipt, never "first received".
        "app abc running (claimed at boot by its serial check-in, unverified) · last claimed 3 s ago",
        "Interrupted (Central's inference: a later boot was admitted)",
        "Unknown: no layer observes them",
    ]


def test_a_fact_missing_its_label_becomes_unknown_naming_what_is_missing():
    out = _run()
    assert out["missing"] == [
        "Unknown: host_observation.received_at not served",
        "Unknown: Host Management receipt time not served",
        "Unknown: Central's read time is not served",
        "Unknown: which Host Management receipt this is is not named",
        "Unknown: the reporting layer is not named",
        "Unknown: what App Effect Broker reported is not served",
        'Unknown: who claimed "Serial x" is not named',
        'Unknown: which receipt of "Boot b-1" this is is not named',
        'Unknown: the basis for "Interrupted" is not named',
        "Unknown: the record's value is not served",
        "Unknown: the fact's kind is not named",
        "Unknown: no reason given",
    ]
    assert out["neverThrows"] == ["unknown"] * 5
    assert out["frozen"] is True
    assert out["notAFact"] == ["Unknown: not a fact", "Unknown: not a fact"]


def test_players_by_device_has_one_row_per_box_including_boxes_seen_only_at_boot():
    out = _run()
    assert out["rows"] == [
        {"deviceId": "device-a", "player": "p-a", "standing": "unbound",
         "standingLabel": "Unbound · enrolled 16 min ago", "name": "Player …ffee42",
         "frames": []},
        {"deviceId": "device-b", "player": "p-b", "standing": "bound",
         "standingLabel": "Bound · 1 of 2 outputs free", "name": "Player device-b",
         "frames": [{"frameId": "lobby", "outputId": "HDMI-A-1"}]},
        {"deviceId": "device-r", "player": "p-r", "standing": "retired",
         "standingLabel": "Retired 1 min ago", "name": "Player device-r", "frames": []},
        {"deviceId": "device-y", "player": None, "standing": "not-enrolled",
         "standingLabel": "Not enrolled · seen at boot, never enrolled", "name": "Player device-y",
         "frames": []},
        {"deviceId": "device-z", "player": None, "standing": "not-enrolled",
         "standingLabel": "Not enrolled · seen at boot, never enrolled", "name": "Player …ffee99",
         "frames": []},
    ]
    # Without boot facts the enrolled boxes remain; nothing is invented.
    assert out["noBootFacts"] == ["device-a", "device-b", "device-r"]
    assert out["empty"] == []


def test_layer_evidence_has_three_labelled_rows_bottom_up():
    """S4c-2a-iii: the host's row reads its line (no session, no host sample); App Manager and the
    App Effect Broker, which only the old path reported, have no row. Mutation probe: bring back
    the App Manager row -> the layer list differs, fails."""
    out = _run()
    layers = out["layers"]
    assert [(row["layer"], row["level"]) for row in layers] == [
        ("Host Management", "L0"), ("Display Host", "L1.5"), ("Player app", "L2")]
    assert layers[0]["facts"] == [["Last reported", "Host Management last reported 4 s ago"]]
    assert layers[0]["details"] == [
        "Line start start-2",
        "Source: Host Management's line as Central keeps it. It states a value only when it changes."]
    # A Central that does not serve the display read: Unknown, naming the field.
    assert layers[1]["facts"] == [["Last reported", "Unknown: display_outputs not served"]]
    assert layers[2]["facts"] == [
        ["Last reported", "Unknown: no readiness report on the current enrollment (epoch 3)"]]
    assert "Enrolled 10 min ago (enrollment is not a report)" in layers[2]["details"]


def test_display_host_row_reads_the_newest_exchange_per_output():
    out = _run()
    display = out["display"]
    # Last reported: the newest exchange receipt across the boot (1997 against read 2000).
    assert display["facts"][0] == ["Last reported", "Display Host last reported 3 s ago"]
    assert display["facts"][1:] == [
        ["Output HDMI-A-1", "Display Host last reported 3 s ago · Panel connector: connected"],
        ["Output HDMI-A-1", "Display Host last reported 3 s ago · Admitted surface: the app's surface "
                            "for Frame lobby (binding generation 3)"],
        ["Output HDMI-A-1", "Display Host last reported 3 s ago · Compositor receipt for that surface, "
                            "sampled 2 s before this report"],
        ["Output HDMI-A-2", "Display Host last reported 10 s ago · Panel connector: not connected"],
        ["Output HDMI-A-2", "Display Host last reported 10 s ago · Display Host reported no app surface admitted"],
        ["Output HDMI-A-2", "Display Host last reported 10 s ago · No compositor receipt for that surface "
                            "in this report"],
        ["Output HDMI-A-3", "Display Host last reported 15 s ago · Panel connector: connected"],
        ["Output HDMI-A-3", "Display Host last reported 15 s ago · Admitted surface: the app's surface "
                            "for Frame hall (binding generation 1)"],
        ["Output HDMI-A-3", "Display Host last reported 15 s ago · No compositor receipt for that surface "
                            "in this report"],
    ]
    assert [item["outputId"] for item in out["displayModel"]] == ["HDMI-A-1", "HDMI-A-2", "HDMI-A-3"]
    assert [len(item["facts"]) for item in out["displayModel"]] == [3, 3, 3]
    assert out["displayEmpty"] == [["Last reported", "Unknown: Display Host has reported no Output on this boot"]]
    assert out["displayNoTime"] == [["Unknown: display_outputs received_at not served"] * 3]
    assert out["displayUndecodable"] == [
        ["Last reported", "Display Host last reported 1 s ago"],
        ["Output HDMI-A-2", "Display Host last reported 10 s ago · Panel connector: not connected"],
        ["Output HDMI-A-2", "Display Host last reported 10 s ago · Display Host reported no app surface admitted"],
        ["Output HDMI-A-2", "Display Host last reported 10 s ago · No compositor receipt for that surface "
                            "in this report"],
        ["Output HDMI-A-9", "Unknown: Central could not decode Display Host's last exchange for this Output"],
    ]
    # The display wording never names the diagnostic page, and never claims what is seen.
    words = " ".join([text for _, text in display["facts"]] + display["details"]).lower()
    for phrase in ("diagnostic page", "visible", "showing"):
        assert phrase not in words


def test_a_silent_app_with_a_reporting_host_shows_both_ages():
    out = _run()
    # p-b's last readiness report is 10 s old against the snapshot's read time.
    assert out["silentApp"][0] == "Host Management last reported 4 s ago"
    assert out["silentApp"][2] == "Player app last reported 10 s ago"


def test_layer_evidence_names_what_it_cannot_read():
    out = _run()
    assert out["noHostLine"][0] == "Unknown: no Host Management line heard"
    assert out["noHostValue"][0] == "Unknown: host.received_at not served"
    disabled = "Unknown: the node read failed (unanswered)"
    assert out["disabled"][:2] == [disabled] * 2
    assert out["disabled"][2] == "Unknown: no readiness report on the current enrollment (epoch 3)"
    record = "Unknown: no current node record for this box"
    assert out["noRecord"][:2] == [record] * 2
    assert out["notYet"][:2] == ["Unknown: not read yet"] * 2
    assert out["notEnrolled"] == [["Last reported", "Unknown: this box has not enrolled"]]
    assert out["retiredApp"] == [
        ["Last reported", "Unknown: Central does not read reports from a retired Player"]]
    assert out["drift"] == "threw"


def test_the_host_start_is_the_lines_or_none():
    out = _run()
    assert out["start"] == [
        {"epoch": "start-2", "text": "Host Management reported line start start-2 · first received 10 min ago"},
        {"epoch": None, "text": "No Host Management line heard"},
        {"epoch": None, "text": "Unknown: the node read failed (unanswered)"},
    ]


def test_no_console_string_names_the_retired_equipment_page():
    # The boxes' home is Players (console DDD §9): nothing tells the operator to refresh or
    # visit an "Equipment" page, and no route table or region is labelled with it.
    offenders = []
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx"), SRC / "routeSamples.json"]:
        text = module.read_text()
        for phrase in ("Refresh Equipment", 'label: "Equipment"', 'aria-label="Equipment"',
                       ">Equipment<", '"#/equipment"'):
            if phrase in text:
                offenders.append((module.name, phrase))
    assert offenders == []


def test_no_console_string_words_central_intent_as_device_truth():
    # Console DDD B3: a Binding is not what a Panel shows, a compositor receipt is not the
    # display, the bound Output is not "display equipment", and no control creates a
    # maintenance request nothing executes.
    offenders = []
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]:
        text = module.read_text()
        for phrase in ("Shows frame", "presented on the display", "Display equipment",
                       "Queue online update", "queueMaintenanceRequest", "heard from`"):
            if phrase in text:
                offenders.append((module.name, phrase))
    assert offenders == []
    assert not (SRC / "PlayerVersions.jsx").exists()


def test_the_panel_at_enrollment_has_one_wording_and_its_true_truth_kind():
    assert _run()["panel"] == [
        "Player app reported Panel connected at the Player app's last enrollment (may be stale) "
        "· first received 1 min ago",
        "No Panel listed as connected at the Player app's last enrollment (may be stale) · recorded 1 min ago",
        "set",
        "Unknown: Central holds no Panel record from the Player app's last enrollment",
    ]


def test_the_node_api_link_reads_as_centrals_inference_or_unknown():
    # Linked and not linked on a reachable hub; one wording per other hub state; a reachable hub
    # that has not seen this Node; then the unlabelled reads, each unknown naming what is missing.
    assert _run()["busLink"] == [
        ["derived", "linked for 3 min (Central's inference: Central's hub holds this Node's leaf)"],
        ["derived", "not linked for 20 s (Central's inference: Central's hub does not hold this Node's leaf)"],
        ["unknown", "Unknown: Central cannot reach its hub"],
        ["unknown", "Unknown: Central's worker is not running"],
        ["unknown", "Unknown: Central runs no hub"],
        ["unknown", "Unknown: Central's hub has not looked for this Node yet"],
        ["unknown", "Unknown: the Node API link is not served"],
        ["unknown", "Unknown: Central's read time is not served"],
        ["unknown", "Unknown: the hub's state is not served"],
    ]
