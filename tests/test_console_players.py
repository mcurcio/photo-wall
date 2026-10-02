"""The fleet views' pure parts (console DDD §5 rule 2, §9, §11; bead B1): facts.js (`fact`,
`factText`), players.js (`playersByDevice`) and nodeRead.js's model (`processFacts`,
`layerEvidence`, `currentSessionBoot`), run under Node as tests/test_console_flow.py runs the
flow kit. Without Node it skips on a developer machine, but FAILS where the checks are meant
to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set). The browser half is
tests/browser/test_player_page_browser.py.
"""

import json
import subprocess
from pathlib import Path

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"
FIXTURE = Path(__file__).parent / "support/node_projection_fixture.json"

SCRIPT = r"""
const facts = await import(process.argv[1]);
const players = await import(process.argv[2]);
const node = await import(process.argv[3]);
const projection = JSON.parse(process.argv[4]).projection;
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
  read_at: 1000, silent_after_seconds: 30, report_interval_seconds: 5,
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

// --- processFacts: the pinned projection fixture, decoded; junk entries left out.
out.processes = node.processFacts({ projection });
out.junk = node.processFacts({ projection: [
  { payload: "not json", fact_index: 0, sequence: 1, received_at: 1 },
  { payload: JSON.stringify({ message: { facts: [{ type: "SurfaceFact" }] } }), fact_index: 0,
    sequence: 2, received_at: 2 },
  ...projection.slice(1),
] }).map((item) => item.sequence);
out.noProjection = node.processFacts({});

// --- layerEvidence: five rows, each fact labelled.
const session = (owner, extra = {}) => ({ session_id: `s-${owner}`, current: true,
  producer: { owner, kernel_boot_id: "boot-2" }, projection: [], host_observation: null,
  manager_preparation: null, ...extra });
const read = { read_at: 2000, boot_claims: [
  { kernel_boot_id: "boot-2", first_received_at: 1400, offer_refusal: null },
  { kernel_boot_id: "boot-1", first_received_at: 100, offer_refusal: null },
], sessions: [
  session("host_core", { host_observation: { received_at: 1996, sample: {
    metrics: [{ name: "uptime", value: 7, unit: "seconds", source: "proc" }], fault_code: null } } }),
  session("app_manager", { manager_preparation: { received_at: 1970, sample: { state: "ready" } } }),
  session("app_effect_broker", { projection }),
  { ...session("host_core"), session_id: "old", current: false },
] };
const reporting = { read, operations: null, readAt: 2000, error: null };
const rows = (nodeDevice, playerId = "p-a") =>
  node.layerEvidence({ nodeDevice, snapshot, playerId }).map((row) => ({
    key: row.key, layer: row.layer, level: row.level,
    facts: row.facts.map((entry) => [entry.label, factText(entry.fact)]),
    details: row.details,
  }));
out.layers = rows(reporting);
out.silentApp = rows(reporting, "p-b").map((row) => row.facts[0][1]);
out.noHostSample = rows({ ...reporting, read: { ...read, sessions: [session("host_core")] } })
  .map((row) => row.facts[0][1]);
// A read refused node_control_disabled mid-session is an ordinary failed read (Part E §25).
out.disabled = rows({ read: null, operations: null, readAt: null,
                      error: { code: "node_control_disabled", status: 503 } }).map((row) => row.facts[0][1]);
out.noRecord = rows({ read: null, operations: null, readAt: null,
                      error: { code: "node_device_unavailable", status: 403 } }).map((row) => row.facts[0][1]);
out.notYet = rows({ read: null, operations: null, readAt: null, error: null })
  .map((row) => row.facts[0][1]);
out.notEnrolled = rows(reporting, null)[4].facts;
// A host that stopped reporting: its session lapsed (current: false), its last sample is served.
const lapsed = (extra) => ({ ...session("host_core", extra), session_id: "s-lapsed", current: false });
out.deadHost = rows({ ...reporting, read: { ...read, read_at: 10000, sessions: [
  lapsed({ host_observation: { received_at: 2800, sample: { metrics: [], fault_code: null } } }),
  { ...lapsed({ host_observation: { received_at: 100, sample: {} } }), session_id: "s-older" },
] } })[0];
out.deadHostNoSample = rows({ ...reporting, read: { ...read, sessions: [lapsed({})] } })[0].facts;
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
out.display = rows(withDisplay(displayOutputs))[3];
out.displayModel = node.displayOutputs(withDisplay(displayOutputs), 2000).map((output) => ({
  outputId: output.outputId, facts: output.facts.map(factText) }));
out.displayEmpty = rows(withDisplay([]))[3].facts;
// An Output Central could not decode (served undecodable): one Unknown fact, the rest unaffected.
out.displayUndecodable = rows(withDisplay([displayOutputs[1],
  { output_id: "HDMI-A-9", received_at: 1999, undecodable: true }]))[3].facts;
out.displayNoTime = node.displayOutputs(withDisplay([{ ...displayOutputs[0], received_at: null }]), 2000)
  .map((output) => output.facts.map(factText));
// Central reads no reports from a retired Player: its null last_report_at is not "no report".
out.retiredApp = rows(reporting, "p-r")[4].facts;
// Payload drift (a field of the wrong type) throws, for the section's boundary to contain.
out.drift = (() => {
  try { node.layerEvidence({ nodeDevice: { ...reporting, read: { ...read, sessions: 5 } }, snapshot,
                             playerId: "p-a" }); return "rendered"; }
  catch { return "threw"; }
})();

// --- currentSessionBoot: the current session's boot as claimed, never linked to a request.
const boot = (nodeDevice) => {
  const result = node.currentSessionBoot(nodeDevice);
  return { kernelBootId: result.kernelBootId ?? null, text: factText(result.fact ?? result.none) };
};
out.boot = [
  boot(reporting),
  boot({ ...reporting, read: { ...read, sessions: [{ ...session("host_core"), current: false }] } }),
  boot({ read: null, error: { code: "node_control_disabled", status: 503 } }),
];
// --- panelAtEnrollment: one wording; connected=false is Central's own record, not a report.
out.panel = [
  factText(players.panelAtEnrollment({ connected: true, width_px: 1920 }, 160, 100)),
  factText(players.panelAtEnrollment({ connected: false }, 160, 100)),
  players.panelAtEnrollment({ connected: false }, 160, 100).kind,
  factText(players.panelAtEnrollment(null, 160, 100)),
];
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "facts.js").as_uri(),
         (SRC / "players.js").as_uri(), (SRC / "nodeRead.js").as_uri(), FIXTURE.read_text()],
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


def test_process_facts_decode_the_pinned_projection_fixture():
    out = _run()
    assert out["processes"] == [
        {"pid": 101, "startTicks": 12, "invocationId": "00000000-0000-0000-0000-000000000388",
         "appEpoch": 2, "environmentSha256": "b" * 64, "state": "running", "sequence": 9,
         "receivedAt": 1100},
        {"pid": 100, "startTicks": 11, "invocationId": "00000000-0000-0000-0000-000000000387",
         "appEpoch": 1, "environmentSha256": "a" * 64, "state": "exited", "sequence": 3,
         "receivedAt": 1000},
    ]
    assert out["junk"] == [9]
    assert out["noProjection"] == []


def test_layer_evidence_has_five_labelled_rows_bottom_up():
    out = _run()
    layers = out["layers"]
    assert [(row["layer"], row["level"]) for row in layers] == [
        ("Host Management", "L0"), ("App Manager", "L1"), ("App Effect Broker", "L1"),
        ("Display Host", "L1.5"), ("Player app", "L2")]
    assert layers[0]["facts"] == [["Last reported", "Host Management last reported 4 s ago"]]
    assert "uptime: 7 seconds (proc)" in layers[0]["details"]
    assert layers[1]["facts"] == [
        ["Last reported", "App Manager last reported 30 s ago · preparation ready"]]
    assert layers[2]["facts"] == [
        ["Last reported", "Unknown: App Effect Broker sends evidence only on change, and Central "
                          "stores no receipt of its polls"],
        # The newest process fact, first received (ages from Central's clock only).
        ["App process", "App Effect Broker reported the app running · first received 15 min ago"],
    ]
    # A Central that does not serve the display read: Unknown, naming the field.
    assert layers[3]["facts"] == [["Last reported", "Unknown: display_outputs not served"]]
    assert layers[4]["facts"] == [
        ["Last reported", "Unknown: no readiness report on the current enrollment (epoch 3)"]]
    assert "Enrolled 10 min ago (enrollment is not a report)" in layers[4]["details"]


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
    assert out["silentApp"][4] == "Player app last reported 10 s ago"


def test_layer_evidence_names_what_it_cannot_read():
    out = _run()
    assert out["noHostSample"][0] == "Unknown: host_observation.received_at not served"
    assert out["noHostSample"][1] == "Unknown: no current App Manager session"
    disabled = "Unknown: the node read failed (node_control_disabled)"
    assert out["disabled"][:4] == [disabled] * 4
    assert out["disabled"][4] == "Unknown: no readiness report on the current enrollment (epoch 3)"
    record = "Unknown: no current node record for this box"
    assert out["noRecord"][:4] == [record] * 4
    assert out["notYet"][:4] == ["Unknown: not read yet"] * 4
    assert out["notEnrolled"] == [["Last reported", "Unknown: this box has not enrolled"]]
    assert out["retiredApp"] == [
        ["Last reported", "Unknown: Central does not read reports from a retired Player"]]
    assert out["drift"] == "threw"


def test_a_dead_host_keeps_its_last_report_time_after_its_session_lapses():
    out = _run()
    # The newest lapsed session's sample speaks (2 h old), never Unknown, and says none is current.
    assert out["deadHost"]["facts"] == [
        ["Last reported", "Host Management last reported 2 h ago"],
        ["Session", "No current Host Management session; the evidence above is from its last session"],
    ]
    assert "Last session s-lapsed · boot boot-2" in out["deadHost"]["details"]
    # Unknown only when no session of the layer holds a sample.
    assert out["deadHostNoSample"] == [["Last reported", "Unknown: no current Host Management session"]]


def test_the_current_session_boot_is_a_claim_or_no_current_session():
    out = _run()
    assert out["boot"] == [
        {"kernelBootId": "boot-2",
         "text": "Boot boot-2 (claimed at boot by the box, unverified) · first received 10 min ago"},
        {"kernelBootId": None, "text": "No current node session"},
        {"kernelBootId": None, "text": "Unknown: the node read failed (node_control_disabled)"},
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
