"""The host-health classifier's pure parts (console DDD §62-§63, bead T1): hostHealth.js
`classifyHost`, `hostIncidents`, `hostRow` and `playersTable` (G2), run under Node as
tests/test_console_players.py runs the fleet model. Bands and the silence limit come only from
the served numbers; the browser half is tests/browser/test_host_health_browser.py."""

import json
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const health = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const { classifyHost, hostIncidents, hostChip, incidentSeverity, judgeHost } = health;
const THRESHOLDS = { host_silent_after_seconds: 60,
  metrics: [{ name: "soc_temperature", unit: "celsius", notice_at: 75, alarm_at: 80 }] };
const read = (devices, thresholds = THRESHOLDS) => ({ read_at: 1000, thresholds, devices });
const sample = (receivedAt, metrics) => ({ received_at: receivedAt, fault_code: null, metrics });
const temp = (value) => ({ name: "soc_temperature", value, unit: "celsius", source: "host_sampler" });
const row = (fields) => ({ device_id: "device-a", host: null, previous_boot_received_at: null,
  intake_full: false, ...fields });
const view = (r, doc) => {
  const c = classifyHost(r, doc);
  return { state: c.state, severity: c.severity,
    worst: c.worst === null ? null : [c.worst.severity, factText(c.worst.fact)],
    items: c.items.map((i) => [i.name, i.band, factText(i.fact) + (i.suffix ? ` · ${i.suffix}` : "")]) };
};
const out = {};
out.notRead = view(null, read([]));
out.never = view(row({}), read([]));
out.hot = view(row({ host: sample(996, [temp(81.2)]) }), read([]));
out.warm = view(row({ host: sample(996, [temp(76)]) }), read([]));
out.cool = view(row({ host: sample(996, [temp(62)]) }), read([]));
out.missing = view(row({ host: sample(996, []) }), read([]));
out.twice = view(row({ host: sample(996, [temp(60), temp(61)]) }), read([]));
out.emptyTable = view(row({ host: sample(996, [temp(95)]) }),
  read([], { host_silent_after_seconds: 60, metrics: [] }));
out.noThresholds = view(row({ host: sample(100, [temp(95)]) }), read([], {}));
out.unitMismatch = view(row({ host: sample(996, [{ ...temp(95), unit: "fahrenheit" }]) }), read([]));
out.silentHot = view(row({ host: sample(880, [temp(95)]) }), read([]));
out.silentLimit = view(row({ host: sample(880, [temp(95)]) }),
  read([], { ...THRESHOLDS, host_silent_after_seconds: 150 }));
out.silentPrevious = view(row({ previous_boot_received_at: 880 }), read([]));
out.refused = view(row({ host: sample(880, [temp(95)]), intake_full: true }), read([]));
out.notYet = view(row({ previous_boot_received_at: 960 }), read([]));
out.clockStep = view(row({ host: sample(1010, [temp(50)]) }), read([]));

const snapshot = { inventory: { read_at: 1000, frames: [{ id: "lobby-left", player_id: "p-a",
    output_id: "HDMI-A-1" }],
  players: [
    { id: "p-a", device_id: "device-a", registered_at: 1, last_seen: 1, retired_at: null },
    { id: "p-b", device_id: "device-b", registered_at: 2, last_seen: 2, retired_at: null }],
  outputs: [{ player_id: "p-a", output_id: "HDMI-A-1", observation: { connected: true } },
            { player_id: "p-b", output_id: "HDMI-A-1", observation: { connected: true } }] } };
const silentBoth = read([row({ host: sample(880, [temp(95)]) }),
                         row({ device_id: "device-b", host: sample(880, [temp(95)]) })]);
out.incidents = hostIncidents(snapshot, silentBoth).map(({ key, text, frames, playerHref }) =>
  ({ key, text, frames, playerHref }));
out.noRead = hostIncidents(snapshot, null);
out.reporting = hostIncidents(snapshot, read([row({ host: sample(990, [temp(95)]) })]));

// N1: the firmware flags, CPU, /run free and App Manager's storage refusal.
const FLAG_NAMES = ["under_voltage", "frequency_capped", "throttled", "soft_temperature_limit"];
const FLAG_LIMITS = [
  ...FLAG_NAMES.map((n) => ({ name: `${n}_now`, unit: "boolean", notice_at: null, alarm_at: 1 })),
  ...FLAG_NAMES.map((n) => ({ name: `${n}_occurred`, unit: "boolean", notice_at: 1, alarm_at: null }))];
const N1 = { ...THRESHOLDS, metrics: [...THRESHOLDS.metrics, ...FLAG_LIMITS] };
const flags = (bits) => FLAG_NAMES.flatMap((n, i) => [
  { name: `${n}_now`, value: (bits >> i) & 1, unit: "boolean", source: "firmware" },
  { name: `${n}_occurred`, value: (bits >> (16 + i)) & 1, unit: "boolean", source: "firmware" }]);
const cpu = { name: "cpu_busy", value: 23, unit: "percent", source: "host_sampler" };
const run = { name: "runtime_available", value: 1.2e9, unit: "bytes", source: "host_sampler" };
const named = (r, doc, name) => view(r, doc).items.find((i) => i[0] === name) ?? null;
out.throttledNow = named(row({ host: sample(996, flags(0x50005)) }), read([], N1), "throttling");
out.occurredOnly = named(row({ host: sample(996, flags(0x10000)) }), read([], N1), "throttling");
out.occurredTwo = named(row({ host: sample(996, flags(0x50000)) }), read([], N1), "throttling");
out.noneNow = named(row({ host: sample(996, flags(0)) }), read([], N1), "throttling");
out.flagMissing = named(row({ host: sample(996, flags(0x5).slice(1)) }), read([], N1), "throttling");
out.flagTwice = named(row({ host: sample(996, [...flags(0), flags(4)[4]]) }), read([], N1), "throttling");
out.flagsUnserved = named(row({ host: sample(996, flags(0x50005)) }), read([]), "throttling");
out.throttledSeverity = view(row({ host: sample(996, flags(0x4)) }), read([], N1)).severity;
out.occurredSeverity = view(row({ host: sample(996, flags(0x40000)) }), read([], N1)).severity;
out.cpu = named(row({ host: sample(996, [{ ...cpu, value: 100 }]) }),
  read([], { ...N1, metrics: [...N1.metrics, { name: "cpu_busy", unit: "percent", notice_at: 1, alarm_at: 2 }] }),
  "cpu_busy");
out.cpuTwice = named(row({ host: sample(996, [cpu, cpu]) }), read([], N1), "cpu_busy");
out.storage = named(row({ host: sample(996, [run]) }), read([], N1), "runtime_available");
const refusal = { received_at: 994, state: "refused", fault: "node_storage_capacity",
  available_bytes: 9e8, required_bytes: 1.4e9 };
out.refusal = named(row({ host: sample(996, [run]), preparation: refusal }), read([], N1), "preparation");
out.refusalSeverity = view(row({ host: sample(996, [run]), preparation: refusal }), read([], N1)).severity;
out.idle = named(row({ host: sample(996, [run]), preparation: { ...refusal, state: "idle", fault: null } }),
  read([], N1), "preparation");
out.otherFault = named(row({ host: sample(996, [run]),
  preparation: { ...refusal, fault: "manager_preparation_fault" } }), read([], N1), "preparation");
out.oneNumber = named(row({ host: sample(996, [run]), preparation: { ...refusal, available_bytes: null } }),
  read([], N1), "preparation");
// Fix 1: a full App Manager intake: the (possibly frozen) newest sample is not judged.
out.prepFull = named(row({ host: sample(996, [run]), preparation: refusal, preparation_intake_full: true }),
  read([], N1), "preparation");
out.prepFullSeverity = view(row({ host: sample(996, [run]), preparation: refusal,
  preparation_intake_full: true }), read([], N1)).severity;
out.prepFullIdle = named(row({ host: sample(996, [run]), preparation: { ...refusal, state: "idle", fault: null },
  preparation_intake_full: true }), read([], N1), "preparation");
out.silentRefusal = named(row({ host: sample(880, [run]), preparation: refusal }), read([], N1), "preparation");

// A1: each incident kind from its own row, for the Bound device-a, with the Unbound device-b
// sent the same row (it must raise nothing).
const both = (fields, doc = N1) => hostIncidents(snapshot, read([row(fields),
  row({ ...fields, device_id: "device-b" })], doc)).map(({ key, severity, text }) => [key, severity, text]);
out.a1 = {
  silent: both({ host: sample(880, [temp(95)]) }),
  silentThrottled: both({ host: sample(880, [temp(95), ...flags(0x4)]) }),
  refused: both({ host: sample(880, flags(0x4)), intake_full: true }),
  never: both({}),
  notRead: hostIncidents(snapshot, read([], N1)),
  previousSilent: both({ previous_boot_received_at: 880 }),
  previousRecent: both({ previous_boot_received_at: 990 }),
  throttled: both({ host: sample(996, flags(0x4)) }),
  hot: both({ host: sample(996, [temp(82)]) }),
  warm: both({ host: sample(996, [temp(76)]) }),
  occurred: both({ host: sample(996, flags(0x40000)) }),
  storage: both({ host: sample(996, [run]), preparation: refusal }),
  hotAndStorage: both({ host: sample(996, [temp(82), ...flags(0x5)]), preparation: refusal }),
};
// Fix 1: the strip's tier is its worst incident; an Unknown-only Player is not an alarm.
const incidentsOf = (fields) => hostIncidents(snapshot, read([row(fields)], N1));
out.tiers = {
  never: incidentSeverity(incidentsOf({})),
  silent: incidentSeverity(incidentsOf({ host: sample(880, [temp(95)]) })),
  mixed: incidentSeverity([...incidentsOf({}), ...incidentsOf({ host: sample(996, flags(0x4)) })]),
  none: incidentSeverity([]),
};
// Fix 1: judgeHost words a box from FleetHosts; a failed read with no earlier result names it.
const judge = (hosts) => { const j = judgeHost(hosts, "device-a");
  return { state: j.health.state, texts: [...new Set(j.health.items.map((i) => factText(i.fact)))],
    facts: [factText(j.facts.receipt), ...j.facts.items.map((i) => factText(i.fact))] }; };
out.judged = {
  failedNoRead: judge({ read: null, failed: true, error: null }),
  notLoaded: judge({ read: null, failed: false, error: null }),
  failedKept: judge({ read: read([row({ host: sample(996, [temp(62)]) })], N1), failed: true, error: null }),
  absent: judge({ read: read([], N1), failed: false, error: null }),
  silentFacts: judge({ read: read([row({ host: sample(880, [temp(62)]), boot: null,
    facts: { first_received_at: 700, kernel_release: null, interface: "eth0", link_state: "up",
      address: null } })], N1), failed: false, error: null }),
};
const chip = (fields, doc = N1) => hostChip("pi-07", classifyHost(row(fields), read([], doc)));
out.chips = {
  throttled: chip({ host: sample(996, [temp(82), ...flags(0x4)]) }),
  silent: chip({ host: sample(820, flags(0x4)) }),
  ok: chip({ host: sample(997, [temp(50)]) }),
  warm: chip({ host: sample(997, [temp(76)]) }),
  refused: chip({ host: sample(880, []), intake_full: true }),
  never: chip({}),
};
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT,
                             str(SRC / "hostHealth.js"), str(SRC / "facts.js")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_states_bands_and_wording():
    out = _run()
    unknown_not_read = "Unknown: not read"
    assert out["notRead"]["state"] == "not_read" and out["notRead"]["severity"] == "unknown"
    assert {text for _, _, text in out["notRead"]["items"]} == {unknown_not_read}
    assert out["never"]["state"] == "never"
    assert out["never"]["items"][0][2] == "Unknown: no Host Management report from this boot or the one before"

    assert out["hot"]["state"] == "reporting" and out["hot"]["severity"] == "alarm"
    assert out["hot"]["items"][:2] == [
        ["host", None, "Host Management last reported 4 s ago"],
        ["soc_temperature", "alarm",
         "81.2 °C · hot (Central's inference: at or above 80 °C, Central's threshold)"]]
    assert out["warm"]["items"][1] == [
        "soc_temperature", "notice",
        "76 °C · warm (Central's inference: at or above 75 °C, Central's threshold)"]
    assert out["cool"]["severity"] == "ok" and out["cool"]["worst"] is None
    assert out["cool"]["items"][1] == ["soc_temperature", "ok", "Host Management last reported 4 s ago · 62 °C"]
    assert out["missing"]["items"][1][2] == "Unknown: not reported" and out["missing"]["severity"] == "ok"
    assert out["twice"]["items"][1][2] == "Unknown: two values reported"


def test_no_served_threshold_means_no_band_and_no_incident():
    out = _run()
    # No console default: 95 °C with an empty table, no table at all, or a unit mismatch reads
    # as the bare reported value.
    for case in ("emptyTable", "unitMismatch"):
        assert out[case]["severity"] == "ok", case
        assert out[case]["items"][1][1] is None
        assert out[case]["items"][1][2].endswith(" · 95 °C"), case
    # Without a served limit no receipt is judged silent, however old.
    assert out["noThresholds"]["state"] == "reporting" and out["noThresholds"]["severity"] == "ok"
    assert out["noThresholds"]["items"][1][2] == "Host Management last reported 15 min ago · 95 °C"


def test_silence_refusal_and_previous_boot():
    out = _run()
    silent = out["silentHot"]
    assert silent["state"] == "silent" and silent["severity"] == "alarm"
    # One cause only: silence. The last value reads "at last report", unbanded.
    assert silent["worst"] == ["alarm", "Host Management silent · last reported 2 min ago "
                               "(Central's inference: no report for over 60 s, Central's limit)"]
    assert silent["items"][1] == ["soc_temperature", None,
                                  "Host Management last reported 2 min ago · 95 °C at last report"]
    # The served limit composes the wording and moves the judgement.
    assert out["silentLimit"]["state"] == "reporting"
    previous = out["silentPrevious"]
    assert previous["state"] == "silent"
    assert previous["worst"][1].startswith(
        "Host Management silent · the previous boot's Host Management last reported 2 min ago")
    assert previous["items"][1][2] == "Unknown: no Host Management sample from this boot"
    refused = out["refused"]
    assert refused["state"] == "refused"
    assert refused["items"][0][2] == ("Central refused Host Management reports today (Central's "
                                      "inference: its daily intake cap for this box is full) · "
                                      "last stored 2 min ago")
    assert out["notYet"]["state"] == "not_yet_this_boot"
    assert out["notYet"]["items"][1][2] == (
        "Unknown: on this boot · the previous boot's Host Management last reported 40 s ago")
    # A receipt in Central's future (a clock step) clamps at 0 and reports.
    assert out["clockStep"]["state"] == "reporting"


def test_incidents_name_the_bound_player_and_link_it():
    out = _run()
    assert out["incidents"] == [{
        "key": "player:device-a:host", "frames": ["lobby-left"], "playerHref": "#/players/device-a",
        "text": "Player device-a (Frame lobby-left) — Host Management silent · last reported 2 min ago"}]
    assert out["noRead"] == []
    # A Reporting 95 °C is a threshold incident (A1), not silence.
    assert [incident["key"] for incident in out["reporting"]] == ["player:device-a:soc_temperature"]


def test_throttling_cpu_and_storage():
    out = _run()
    basis = "(Central's inference: the firmware flag is set, Central's threshold)"
    assert out["throttledNow"] == ["throttling", "alarm", f"Throttled now · Under-voltage now {basis}"]
    assert out["occurredOnly"] == ["throttling", "notice", "None now · under-voltage occurred recently "
                                   f"(the firmware's sticky flag) {basis}"]
    assert out["occurredTwo"][2].startswith("None now · under-voltage, throttling occurred recently "
                                            "(the firmware's sticky flags)")
    assert out["noneNow"] == ["throttling", "ok", "Host Management last reported 4 s ago · None now"]
    assert out["flagMissing"] == ["throttling", "unknown", "Unknown: not reported"]
    assert out["flagTwice"] == ["throttling", "unknown", "Unknown: two values reported"]
    # No served flag threshold: the value alone, no band (no console default).
    assert out["flagsUnserved"] == ["throttling", None,
                                    "Host Management last reported 4 s ago · Throttled now · Under-voltage now"]
    assert out["throttledSeverity"] == "alarm" and out["occurredSeverity"] == "notice"
    # CPU is never banded, even when a threshold is served for it.
    assert out["cpu"] == ["cpu_busy", None, "Host Management last reported 4 s ago · 100 % busy"]
    assert out["cpuTwice"] == ["cpu_busy", "unknown", "Unknown: two values reported"]
    assert out["storage"] == ["runtime_available", None, "Host Management last reported 4 s ago · 1.2 GB free in /run"]
    assert out["refusal"] == ["preparation", "alarm", "App Manager last reported 6 s ago · App Manager "
                              "refused a preparation: needs 1.4 GB, room 0.9 GB"]
    assert out["refusalSeverity"] == "alarm"
    # A newer sample, another fault, a missing number or a silent host: no storage item.
    for case in ("idle", "otherFault", "oneNumber", "silentRefusal"):
        assert out[case] is None, case


FACTS_SCRIPT = r"""
const { hostFactItems } = await import(process.argv[1]);
const { factText, receiptText } = await import(process.argv[2]);
const FACTS = { first_received_at: 1000 - 3 * 86400, kernel_release: "6.6.51+rpt-rpi-v8",
  interface: "eth0", link_state: "up", address: "192.168.1.40", base_tag: "2026.10.01" };
const LIMITED = { read_at: 1000, thresholds: { host_silent_after_seconds: 60, metrics: [] } };
const row = (fields) => ({ device_id: "device-a", host: null, previous_boot_received_at: null,
  intake_full: false, boot: { base_tag: "2026.10.01" }, facts: FACTS, ...fields });
const view = (r, doc = { read_at: 1000 }) => {
  const { receipt, items } = hostFactItems(r, doc);
  return { receipt: [receipt.kind, receiptText(receipt) ?? factText(receipt)],
    items: items.map((i) => [i.name, i.label, i.fact.kind, factText(i.fact, { receipt: false })]) };
};
console.log(JSON.stringify({
  full: view(row({})),
  noFacts: view(row({ facts: null })),
  absent: view(row({ facts: { ...FACTS, kernel_release: null, address: null, link_state: null } })),
  noInterface: view(row({ facts: { ...FACTS, interface: null } })),
  noBoot: view(row({ boot: null })),
  noTag: view(row({ boot: { base_tag: null } })),
  differ: view(row({ facts: { ...FACTS, base_tag: "2026.09.30" } })),
  noReportedBase: view(row({ facts: { ...FACTS, base_tag: null } })),
  differNoOffer: view(row({ boot: { base_tag: null }, facts: { ...FACTS, base_tag: "2026.09.30" } })),
  notRead: view(null),
  reporting: view(row({ host: { received_at: 990, metrics: [], fault_code: null } }), LIMITED),
  silent: view(row({ host: { received_at: 1000 - 3 * 3600, metrics: [], fault_code: null } }), LIMITED),
  refused: view(row({ host: { received_at: 880, metrics: [], fault_code: null }, intake_full: true }),
    LIMITED),
}));
"""


def test_host_facts_and_the_base_are_worded_once_per_record():
    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", FACTS_SCRIPT,
                             str(SRC / "hostHealth.js"), str(SRC / "facts.js")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    base = ["base", "Software", "claimed",
            "Central's offer: base 2026.10.01 (claimed at boot by this boot's node session, unverified)"]
    reported = ["base_reported", "Software", "reported", "Host Management reported base 2026.10.01"]
    assert out["full"] == {"receipt": ["reported", "first received 3 d ago"], "items": [
        ["link", "Network", "reported", "Host Management reported eth0 up"],
        ["address", "Network", "reported", "Host Management reported address 192.168.1.40"],
        ["kernel", "Software", "reported", "Host Management reported kernel 6.6.51+rpt-rpi-v8"],
        reported, base]}
    assert out["noFacts"] == {"receipt": ["unknown", "Unknown: no host facts received on this boot"],
                              "items": [base]}
    assert [item[3] for item in out["absent"]["items"]] == [
        "Host Management reported eth0",
        "Unknown: Host Management could not read the link state of eth0",
        "Unknown: Host Management could not read the address",
        "Unknown: Host Management could not read the kernel release", reported[3], base[3]]
    assert out["noInterface"]["items"][0][3] == (
        "Unknown: Host Management could not read the default-route interface")
    assert out["noBoot"]["items"][-1][3] == "Unknown: no current node boot admission"
    assert out["noTag"]["items"][-1][3] == "Unknown: this boot's offer names no base tag"
    assert out["notRead"] == {"receipt": ["unknown", "Unknown: not read"],
                              "items": [["base", "Software", "unknown", "Unknown: not read"]]}
    # The node's report and Central's offer sit side by side; differing tags add one derived
    # fact naming both, with no band. A missing side never derives a mismatch.
    assert out["differ"]["items"][-3:] == [
        ["base_reported", "Software", "reported", "Host Management reported base 2026.09.30"], base,
        ["base_mismatch", "Software", "derived", "Base differs: Host Management reported 2026.09.30, "
         "Central's offer 2026.10.01 (Central's inference: the reported tag and the offered tag differ)"]]
    assert out["noReportedBase"]["items"][-2:] == [
        ["base_reported", "Software", "unknown", "Unknown: Host Management could not read the base tag"], base]
    assert [item[0] for item in out["differNoOffer"]["items"]][-2:] == ["base_reported", "base"]
    # One gate words the values and the facts: a box Central cannot hear never reads its link
    # in the present tense (§62 Silent row, §66).
    assert out["reporting"] == out["full"]
    last = ["Host Management reported eth0 up at last report",
            "Host Management reported address 192.168.1.40 at last report",
            "Host Management reported kernel 6.6.51+rpt-rpi-v8 at last report",
            "Host Management reported base 2026.10.01 at last report", base[3]]
    for state in ("silent", "refused"):
        assert [item[3] for item in out[state]["items"]] == last, state
        assert out[state]["receipt"] == ["reported", "first received 3 d ago"], state


TABLE_SCRIPT = r"""
const { playersTable } = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const THRESHOLDS = { host_silent_after_seconds: 60,
  metrics: [{ name: "soc_temperature", unit: "celsius", notice_at: 75, alarm_at: 80 }] };
const temp = (value) => ({ name: "soc_temperature", value, unit: "celsius", source: "host_sampler" });
const host = (id, receivedAt, value) => ({ device_id: id, previous_boot_received_at: null,
  intake_full: false, host: { received_at: receivedAt, fault_code: null, metrics: [temp(value)] } });
const box = (id, standing) => ({ deviceId: id, name: `Player ${id}`, standing, frames: [] });
const hosts = { failed: false, error: null, read: { read_at: 1000, thresholds: THRESHOLDS, devices: [
  host("a-ok", 999, 50), host("b-hot", 999, 81), host("c-spare-silent", 100, 50),
  host("d-spare-warm", 999, 76), host("z-silent", 100, 50)] } };
const rows = [box("a-ok", "bound"), box("r-retired", "retired"), box("c-spare-silent", "unbound"),
  box("b-hot", "bound"), box("d-spare-warm", "not-enrolled"), box("z-silent", "bound")];
const view = (list) => list.map((e) => [e.row.deviceId, e.group, e.tier,
  e.health === null ? null : e.health.items.filter((i) => i.band === "alarm" || i.band === "notice")
    .map((i) => i.name),
  e.health === null ? null : e.health.items.find((i) => i.name === "host").fact.kind,
  e.health === null ? null : e.health.items.some((i) => i.band !== null)]);
const words = (list) => Object.fromEntries(list.filter((e) => e.group === "spare").map((e) =>
  [e.row.deviceId, [...e.health.items.map((i) => factText(i.fact)), ...e.facts.items.map((i) => factText(i.fact))]]));
console.log(JSON.stringify({ judged: view(playersTable(rows, hosts)), skipped: view(playersTable(rows, null)),
  spareWords: words(playersTable(rows, hosts)) }));
"""


def test_the_players_table_tiers_only_bound_players_and_puts_spares_then_retired_last():
    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", TABLE_SCRIPT, str(SRC / "hostHealth.js"),
                             str(SRC / "facts.js")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # Bound rows worst first (alarm before ok); spares unbanded and untiered below them, though
    # one is silent (its silence reads as a plain receipt age) and one warm; retired last and
    # not judged at all (G2: a spare is never alarmed; a retired box is never "Unknown: not read").
    assert out["judged"] == [
        ["b-hot", "bound", "alarm", ["soc_temperature"], "reported", True],
        ["z-silent", "bound", "alarm", ["host"], "derived", True],
        ["a-ok", "bound", "ok", [], "reported", True],
        ["c-spare-silent", "spare", None, [], "reported", False],
        ["d-spare-warm", "spare", None, [], "reported", False],
        ["r-retired", "retired", None, None, None, None]]
    assert [entry[:3] for entry in out["skipped"]] == [
        ["a-ok", "bound", None], ["b-hot", "bound", None], ["z-silent", "bound", None],
        ["c-spare-silent", "spare", None], ["d-spare-warm", "spare", None],
        ["r-retired", "retired", None]]
    # The words, not the class: no threshold judgement of Central's reaches a spare's text.
    spare = out["spareWords"]
    assert set(spare) == {"c-spare-silent", "d-spare-warm"}
    for device, texts in spare.items():
        text = " | ".join(texts)
        for judgement in ("hot", "warm", "silent", "at last report", "Central's threshold",
                          "Central's limit", "Central's inference"):
            assert judgement not in text, (device, judgement, text)
    assert "Host Management last reported 15 min ago" in spare["c-spare-silent"][0]
    assert "76 °C" in " ".join(spare["d-spare-warm"])


def test_each_host_incident_kind_comes_from_its_own_row_for_bound_players_only():
    out = _run()["a1"]
    who = "Player device-a (Frame lobby-left) — "
    silence = ["player:device-a:host", "alarm",
               f"{who}Host Management silent · last reported 2 min ago"]
    assert out["silent"] == [silence]
    # A host that died throttled: silence only, never a "now" incident from its last sample.
    assert out["silentThrottled"] == [silence]
    # A full intake replaces silence with the refusal.
    assert out["refused"] == [["player:device-a:host", "alarm",
                               f"{who}Central refused Host Management reports today"]]
    assert out["never"] == [["player:device-a:host", "unknown",
                             f"{who}Unknown: no Host Management report from this boot or the one before"]]
    assert out["notRead"] == []
    # The previous boot's receipt: silence when old, nothing when recent, never a threshold.
    assert out["previousSilent"] == [["player:device-a:host", "alarm", f"{who}Host Management silent · "
                                      "the previous boot's Host Management last reported 2 min ago"]]
    assert out["previousRecent"] == []
    assert out["throttled"] == [["player:device-a:throttling", "alarm", f"{who}throttled now"]]
    assert out["hot"] == [["player:device-a:soc_temperature", "alarm", f"{who}82 °C · hot"]]
    # Notices raise nothing: a warm temperature, a sticky "occurred" flag.
    assert out["warm"] == [] and out["occurred"] == []
    assert out["storage"] == [["player:device-a:preparation", "alarm",
                               f"{who}App Manager refused a preparation: needs 1.4 GB, room 0.9 GB"]]
    assert [text for _, _, text in out["hotAndStorage"]] == [
        f"{who}82 °C · hot", f"{who}throttled now · under-voltage now",
        f"{who}App Manager refused a preparation: needs 1.4 GB, room 0.9 GB"]


def test_the_status_chip_names_the_worst_item_else_the_receipt():
    out = _run()["chips"]
    assert out["throttled"] == {"text": "pi-07 · 82 °C · hot", "severity": "alarm"}
    assert out["silent"] == {"text": "pi-07 · Host Management silent 3 min", "severity": "alarm"}
    assert out["ok"] == {"text": "pi-07 · Host Management last reported 3 s ago", "severity": "ok"}
    assert out["warm"] == {"text": "pi-07 · 76 °C · warm", "severity": "notice"}
    assert out["refused"]["text"] == "pi-07 · Central refused Host Management reports today"
    assert out["never"] == {"text": "pi-07 · Unknown: no Host Management report from this boot or the one before",
                            "severity": "unknown"}


def test_strip_tier_is_the_worst_incident_and_a_failed_read_is_named():
    out = _run()
    # An Unknown-only Player ("Never reported") is unknown, never an alarm.
    assert out["tiers"] == {"never": "unknown", "silent": "alarm", "mixed": "alarm", "none": "ok"}
    judged = out["judged"]
    failed = "Unknown: the fleet host read failed"
    assert judged["failedNoRead"] == {"state": "not_read", "texts": [failed], "facts": [failed, failed]}
    not_read = "Unknown: not read"
    assert judged["notLoaded"] == {"state": "not_read", "texts": [not_read], "facts": [not_read, not_read]}
    assert judged["absent"] == {"state": "not_read", "texts": [not_read], "facts": [not_read, not_read]}
    # A failed read that kept an earlier result judges those values; the page names the failure.
    assert judged["failedKept"]["state"] == "reporting"
    # The page path takes the same gate: a silent box's link reads as its last report.
    assert judged["silentFacts"]["state"] == "silent"
    assert judged["silentFacts"]["facts"][1].startswith("Host Management reported eth0 up at last report ·")


def test_a_full_app_manager_intake_is_never_judged_from_its_frozen_sample():
    out = _run()
    why = ("Unknown: Central refused App Manager reports today: its daily intake cap for this box "
           "is full")
    # Neither the stored refusal (a false alarm once storage frees) nor a stored idle (a false
    # clear during the shortage) is judged; both read Central's refusal.
    assert out["prepFull"] == ["preparation", "unknown", why]
    assert out["prepFullIdle"] == ["preparation", "unknown", why]
    assert out["prepFullSeverity"] == "ok"


BOOT_SCRIPT = r"""
const { classifyHost, hostIncidents, playersTable } = await import(process.argv[1]);
const { factText } = await import(process.argv[2]);
const THRESHOLDS = { host_silent_after_seconds: 60, metrics: [] };
const read = (devices) => ({ read_at: 1000, thresholds: THRESHOLDS, devices });
const GiB = 2 ** 30;
const stage = (name, state, fault = null, required = null, room = null) =>
  ({ stage: name, state, fault, required_bytes: required, room_bytes: room });
const boot = (stages, units = [], more = 0) => ({ stages, failed_units: units, failed_units_more: more });
const oom = (slice, value) => ({ name: `oom_kill:${slice}`, value, unit: "count", source: "host_core" });
const row = (fields, metrics = [], receivedAt = 996) => ({ device_id: "device-a", previous_boot_received_at: null,
  intake_full: false, host: { received_at: receivedAt, fault_code: null, metrics },
  ...fields });
const withBoot = (report, metrics = [], receivedAt = 996) => row({ facts: { first_received_at: 940,
  kernel_release: null, interface: null, link_state: null, address: null, base_tag: null, boot: report } },
  metrics, receivedAt);
const pick = (r, name) => {
  const c = classifyHost(r, read([]));
  const i = c.items.find((entry) => entry.name === name);
  return [i.band, factText(i.fact), c.severity];
};
const done = ["handoff", "storage", "prepare"].map((n) => stage(n, "done"));
const out = {};
out.memoryClass = pick(withBoot(boot([stage("handoff", "done"),
  stage("storage", "refused", "node_memory_class", 3.5 * GiB, 1.9e9)])), "boot_preparation");
out.prepareRoom = pick(withBoot(boot([stage("handoff", "done"), stage("storage", "done"),
  stage("prepare", "refused", "node_storage_capacity", 2.1e9, 1.8e9)])), "boot_preparation");
out.failed = pick(withBoot(boot([...done.slice(0, 2), stage("prepare", "failed", "os:ENOSPC")])), "boot_preparation");
// Out of boot order in the document, the earlier stage still wins: the cause precedes its effects.
out.firstStopped = pick(withBoot(boot([stage("prepare", "failed", "os:ENOSPC"), stage("handoff", "done"),
  stage("storage", "failed", "node_storage_ownership")])), "boot_preparation");
out.stopBeatsRunning = pick(withBoot(boot([stage("handoff", "running"),
  stage("storage", "failed", "node_storage_ownership")])), "boot_preparation");
out.running = pick(withBoot(boot([...done.slice(0, 2), stage("prepare", "running")])), "boot_preparation");
out.done = pick(withBoot(boot(done)), "boot_preparation");
out.notStarted = pick(withBoot(boot([stage("handoff", "done")])), "boot_preparation");
out.olderNode = pick(withBoot(null), "boot_preparation");
out.noFacts = pick(row({ facts: null }), "base_units");
out.silent = pick(withBoot(boot([stage("storage", "failed", "node_storage_ownership")]), [], 880),
  "boot_preparation");
const units = (list, more = 0, stages = done) => pick(withBoot(boot(stages, list, more)), "base_units");
out.unitsNone = units([]);
// A stage unit is left out only when its stage reads stopped; a done stage's failed unit stays.
out.unitsStageOnly = units(["photo-wall-node-prepare.service"], 0,
  [...done.slice(0, 2), stage("prepare", "failed", "os:ENOSPC")]);
out.unitsDoneStage = units(["photo-wall-node-prepare.service"]);
out.unitsOne = units(["photo-wall-app-broker.service", "photo-wall-node-prepare.service"], 0,
  [...done.slice(0, 2), stage("prepare", "running")]);
// Killed before its exit write (out-of-memory victim, TimeoutStartSec SIGTERM), or the record
// write failed: PID1's failed unit stops the stage, on both boot items.
const killedPrepare = boot([...done.slice(0, 2), stage("prepare", "running")], ["photo-wall-node-prepare.service"]);
const noRecords = boot([], ["photo-wall-node-storage.service"]);
out.killed = { prepare: pick(withBoot(killedPrepare), "boot_preparation"),
  prepareUnits: pick(withBoot(killedPrepare), "base_units"),
  storage: pick(withBoot(noRecords), "boot_preparation"), storageUnits: pick(withBoot(noRecords), "base_units") };
out.unitsMore = units(["photo-wall-app-broker.service", "photo-wall-display.service"], 3);
// Two stopped stages (a failed handoff record, and storage killed with no record): both their
// units are left out, not only the first shown's; the broker's stays.
const twoStopped = (extra) => boot([stage("handoff", "failed", "node_measured_base_abi_mismatch")],
  ["photo-wall-node-handoff.service", "photo-wall-node-storage.service", ...extra]);
out.twoStopped = { units: pick(withBoot(twoStopped([])), "base_units"),
  broker: pick(withBoot(twoStopped(["photo-wall-app-broker.service"])), "base_units"),
  preparation: pick(withBoot(twoStopped([])), "boot_preparation") };
// Errata E-T3-2: units not read (null) are Unknown with no band, never "no base unit failed",
// and a running record stays running: no alarm, no clear.
const unread = (stages) => ({ stages, failed_units: null, failed_units_more: null });
out.unread = { units: pick(withBoot(unread([...done.slice(0, 2), stage("prepare", "running")])), "base_units"),
  running: pick(withBoot(unread([...done.slice(0, 2), stage("prepare", "running")])), "boot_preparation"),
  refused: pick(withBoot(unread([stage("handoff", "done"), stage("storage", "refused", "node_memory_class",
    3.5 * GiB, 1.9e9)])), "boot_preparation") };
out.unitsUnnamed = units([], 2);
out.oom = pick(row({}, [oom("base", 1), oom("app", 2), oom("preparation", 0)]), "out_of_memory");
out.oomNone = pick(row({}, [oom("base", 0), oom("app", 0)]), "out_of_memory");
out.oomAbsent = pick(row({}), "out_of_memory");
out.oomTwice = pick(row({}, [oom("app", 1), oom("app", 2)]), "out_of_memory");
// No row cap hides a kill: the fourth row's kills are shown.
out.oomFourth = pick(row({}, [oom("base", 0), oom("preparation", 0), oom("app", 0), oom("hostcore", 5)]),
  "out_of_memory");
const memcg = (value) => ({ name: "memcg_present", value, unit: "boolean", source: "cgroup" });
out.memcgAbsent = pick(row({}, [memcg(0)]), "memory_limits");
out.memcgOn = pick(row({}, [memcg(1)]), "memory_limits");
out.memcgUnreported = pick(row({}), "memory_limits");

// One cause, one incident: a failed prepare stage whose unit failed too raises only the stage.
const snapshot = { inventory: { read_at: 1000, frames: [{ id: "lobby-left", player_id: "p-a",
    output_id: "HDMI-A-1" }],
  players: [{ id: "p-a", device_id: "device-a", registered_at: 1, last_seen: 1, retired_at: null },
            { id: "p-b", device_id: "device-b", registered_at: 2, last_seen: 2, retired_at: null }],
  outputs: [{ player_id: "p-a", output_id: "HDMI-A-1", observation: { connected: true } }] } };
const failing = boot([...done.slice(0, 2), stage("prepare", "failed", "os:ENOSPC")],
  ["photo-wall-node-prepare.service"]);
const incidents = (report, metrics = []) => hostIncidents(snapshot,
  read([withBoot(report, metrics), { ...withBoot(report, metrics), device_id: "device-b" }]))
  .map(({ key, text }) => [key, text]);
out.incidents = {
  stageAndUnit: incidents(failing),
  refusedAndUnit: incidents(boot([stage("handoff", "done"), stage("storage", "refused", "node_memory_class",
    3.5 * GiB, 1.9e9)], ["photo-wall-node-storage.service"])),
  killedPrepare: incidents(killedPrepare),
  noRecords: incidents(noRecords),
  broker: incidents(boot(done, ["photo-wall-app-broker.service"])),
  oomNotice: incidents(boot(done), [oom("app", 3)]),
  memcgAbsent: incidents(boot(done), [{ name: "memcg_present", value: 0, unit: "boolean", source: "cgroup" }]),
  unitsUnread: incidents({ stages: done, failed_units: null, failed_units_more: null }),
  olderNode: incidents(null),
};
// G2: a spare's stopped stage and failed unit are words only, never a band or a tier.
const spare = playersTable([{ deviceId: "device-a", name: "Player device-a", standing: "unbound", frames: [] }],
  { failed: false, error: null, read: read([withBoot(boot([stage("storage", "refused", "node_memory_class",
    3.5 * GiB, 1.9e9)], ["photo-wall-app-broker.service"]), [oom("app", 1)])]) })[0];
out.spare = { tier: spare.tier, bands: spare.health.items.map((i) => i.band),
  words: spare.health.items.filter((i) => ["boot_preparation", "base_units", "out_of_memory"].includes(i.name))
    .map((i) => factText(i.fact)) };
console.log(JSON.stringify(out));
"""


def _run_boot():
    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", BOOT_SCRIPT,
                             str(SRC / "hostHealth.js"), str(SRC / "facts.js")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_boot_preparation_names_the_first_stopped_stage_in_boot_order():
    out = _run_boot()
    reported = "Host Management reported "
    receipt = " · first received 1 min ago"
    assert out["memoryClass"] == ["alarm", f"{reported}boot preparation refused at storage: needs 3.8 GB "
                                  f"of memory, the box has 1.9 GB{receipt}", "alarm"]
    assert out["prepareRoom"] == ["alarm", f"{reported}boot preparation refused at prepare: needs 2.1 GB, "
                                  f"room 1.8 GB{receipt}", "alarm"]
    assert out["failed"] == ["alarm", f"{reported}boot preparation failed at prepare (os:ENOSPC){receipt}",
                             "alarm"]
    # The cause precedes its effects: boot order, not document order, and a stop before a run.
    assert out["firstStopped"][1] == (f"{reported}boot preparation failed at storage "
                                      f"(node_storage_ownership){receipt}")
    assert out["stopBeatsRunning"][1] == out["firstStopped"][1]
    assert out["running"] == [None, f"{reported}boot preparation running: prepare{receipt}", "ok"]
    assert out["done"] == [None, f"{reported}boot preparation done{receipt}", "ok"]
    assert out["notStarted"] == [None, f"{reported}boot preparation not started{receipt}", "ok"]
    # An older node sends no boot report: Unknown on its item, no tier raised.
    assert out["olderNode"] == ["unknown", "Unknown: not reported", "ok"]
    assert out["noFacts"] == ["unknown", "Unknown: not reported", "ok"]
    # A stage killed before its exit write, or whose record write failed, reads stopped by its
    # failed unit (docs/node-4gb-memory-design.md §6), and the unit is not repeated under base units.
    killed = out["killed"]
    assert killed["prepare"] == ["alarm", f"{reported}boot preparation failed at prepare (unit failed, "
                                 f"no exit record){receipt}", "alarm"]
    assert killed["storage"] == ["alarm", f"{reported}boot preparation failed at storage (unit failed, "
                                 f"no exit record){receipt}", "alarm"]
    assert killed["prepareUnits"][:2] == killed["storageUnits"][:2] == [
        None, f"{reported}no base unit failed on this boot{receipt}"]
    # Silent: the last report, unbanded; only the silence is judged.
    assert out["silent"][0] is None
    assert out["silent"][1].endswith("(node_storage_ownership) at last report" + receipt)


def test_base_units_leave_out_the_stage_units_and_out_of_memory_is_a_notice():
    out = _run_boot()
    reported = "Host Management reported "
    receipt = " · first received 1 min ago"
    assert out["unitsNone"] == [None, f"{reported}no base unit failed on this boot{receipt}", "ok"]
    # The stopped stage's unit is left out; the box's alarm is boot preparation's.
    assert out["unitsStageOnly"][:2] == out["unitsNone"][:2] and out["unitsStageOnly"][2] == "alarm"
    assert out["unitsDoneStage"] == ["alarm", f"{reported}base unit failed on this boot: "
                                     f"photo-wall-node-prepare.service{receipt}", "alarm"]
    assert out["unitsOne"] == ["alarm", f"{reported}base unit failed on this boot: "
                               f"photo-wall-app-broker.service{receipt}", "alarm"]
    assert out["unitsMore"][1] == (f"{reported}base units failed on this boot: photo-wall-app-broker.service"
                                   f" · photo-wall-display.service and 3 more{receipt}")
    assert out["unitsUnnamed"][1] == f"{reported}base units failed on this boot: 2 not named{receipt}"
    latest = "Host Management last reported 4 s ago · "
    assert out["oom"] == ["notice", f"{latest}Out-of-memory kills on this boot: app 2 · base 1", "notice"]
    assert out["oomNone"] == [None, f"{latest}No out-of-memory kills on this boot", "ok"]
    assert out["oomAbsent"] == ["unknown", "Unknown: not reported", "ok"]
    assert out["oomTwice"] == ["unknown", "Unknown: two values reported", "ok"]
    assert out["oomFourth"] == ["notice", f"{latest}Out-of-memory kills on this boot: hostcore 5", "notice"]
    two = out["twoStopped"]
    assert two["units"] == [None, f"{reported}no base unit failed on this boot{receipt}", "alarm"]
    assert two["broker"][:2] == ["alarm", f"{reported}base unit failed on this boot: "
                                 f"photo-wall-app-broker.service{receipt}"]
    assert two["preparation"][1] == (f"{reported}boot preparation failed at handoff "
                                     f"(node_measured_base_abi_mismatch){receipt}")


def test_failed_units_not_read_are_unknown_and_never_clear_or_stop_a_stage():
    out = _run_boot()["unread"]
    reported = "Host Management reported "
    receipt = " · first received 1 min ago"
    assert out["units"] == [None, "Unknown: failed units not read", "ok"]
    assert out["running"] == [None, f"{reported}boot preparation running: prepare{receipt}", "ok"]
    assert out["refused"][0] == "alarm"


def test_an_absent_memory_controller_is_a_warning_never_an_alarm():
    out = _run_boot()
    latest = "Host Management last reported 4 s ago · "
    assert out["memcgAbsent"] == ["notice", f"{latest}memory controller absent: memory limits not enforced",
                                  "notice"]
    assert out["memcgOn"] == [None, f"{latest}memory controller on: memory limits enforced", "ok"]
    assert out["memcgUnreported"] == ["unknown", "Unknown: not reported", "ok"]


def test_one_boot_cause_raises_one_incident_for_bound_players_and_a_spare_is_never_alarmed():
    out = _run_boot()
    who = "Player device-a (Frame lobby-left) — "
    incidents = out["incidents"]
    assert incidents["stageAndUnit"] == [["player:device-a:boot_preparation",
                                          f"{who}boot preparation failed at prepare (os:ENOSPC)"]]
    assert incidents["refusedAndUnit"] == [["player:device-a:boot_preparation",
                                            f"{who}boot preparation refused at storage: needs 3.8 GB of "
                                            "memory, the box has 1.9 GB"]]
    assert incidents["killedPrepare"] == [["player:device-a:boot_preparation",
                                           f"{who}boot preparation failed at prepare (unit failed, no exit record)"]]
    assert incidents["noRecords"] == [["player:device-a:boot_preparation",
                                       f"{who}boot preparation failed at storage (unit failed, no exit record)"]]
    assert incidents["broker"] == [["player:device-a:base_units",
                                    f"{who}base unit failed on this boot: photo-wall-app-broker.service"]]
    assert incidents["oomNotice"] == [] and incidents["olderNode"] == []
    # A warning raises no incident; neither does units Host Management could not read.
    assert incidents["memcgAbsent"] == [] and incidents["unitsUnread"] == []
    spare = out["spare"]
    assert spare["tier"] is None and set(spare["bands"]) == {None}
    assert [text.split(" · ")[0] for text in spare["words"]] == [
        "Host Management last reported 4 s ago",
        "Host Management reported boot preparation refused at storage: needs 3.8 GB of memory, "
        "the box has 1.9 GB",
        "Host Management reported base unit failed on this boot: photo-wall-app-broker.service"]


FAMILIES_SCRIPT = r"""
const { HOST_CATALOG, NOT_SHOWN, BOOT_STAGES, STAGE_UNITS } = await import(process.argv[1]);
console.log(JSON.stringify({ items: Object.fromEntries(Object.entries(HOST_CATALOG).map(([name, entry]) =>
  [name, entry.families])), notShown: NOT_SHOWN, boot: { stages: BOOT_STAGES, units: STAGE_UNITS } }));
"""


def test_every_metric_family_has_a_console_item_or_is_listed_not_shown():
    """Each METRIC_FAMILIES key (contracts/node_observation.py) is read by exactly one
    HOST_CATALOG item or listed in NOT_SHOWN, and neither names a family the contract lacks."""
    from contracts.node_observation import METRIC_FAMILIES

    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", FAMILIES_SCRIPT, str(SRC / "hostHealth.js")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    mapped = [family for families in out["items"].values() for family in families]
    named = mapped + out["notShown"]
    assert len(named) == len(set(named)), "a family is read twice, or both read and not shown"
    assert set(named) == {family.key for family in METRIC_FAMILIES}
    # The boot items read the host facts record, not a metric family.
    assert out["items"]["boot_preparation"] == [] and out["items"]["base_units"] == []
    assert out["items"]["out_of_memory"] == ["oom_kill:"]
    assert out["items"]["memory_limits"] == ["memcg_present"]


def test_the_boot_items_constants_are_the_contracts_and_the_unit_files():
    """hostHealth.js's boot stages and stage unit names equal contracts/node_host_facts.py and
    appliance/systemd."""
    from contracts.node_host_facts import BOOT_STAGES

    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", FAMILIES_SCRIPT, str(SRC / "hostHealth.js")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    boot = json.loads(result.stdout)["boot"]
    assert tuple(boot["stages"]) == BOOT_STAGES
    assert list(boot["units"]) == list(BOOT_STAGES)
    systemd = Path(__file__).resolve().parents[1] / "appliance" / "systemd"
    assert set(boot["units"].values()) == {path.name for path in systemd.glob("photo-wall-node-*.service")}
