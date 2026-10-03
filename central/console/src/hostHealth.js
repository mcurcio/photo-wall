import { fact, factText, LAYER_NAMES } from "./facts.js";
import { hostRow } from "./fleetHosts.js";
import { formatAge, gigabytes } from "./health.js";
import { playersByDevice } from "./players.js";
import { formatRoute } from "./routes.js";

/**
 * Host health (console DDD §62-§63): the ONE classifier of the fleet host read (G12,
 * fleetHosts.js). It puts each box in exactly one state, words every value as a fact
 * (facts.js), and judges bands only from Central's served numbers: the console holds no
 * default threshold and no default silence limit. Every age is Central's read time minus
 * Central's receipt time, through `fact()` (R10).
 *
 * States, checked in this order:
 *   not_read           the box is absent from the read
 *   never              no Host Management receipt served: none from this boot and none from the
 *                      most recently superseded one (G12 sees no older boot)
 *   refused            the newest receipt is older than the limit and Central's intake is full
 *   silent             the newest receipt (this boot's, else the previous boot's) is older than
 *                      the limit
 *   not_yet_this_boot  no sample from this boot; the previous boot's receipt is within it
 *   reporting          this boot's newest sample was received within the limit
 * Bands are judged only while `reporting`. On `silent` and `refused` this boot's values read
 * "at last report", unbanded; the previous boot's values are never shown. Without a served
 * limit no receipt is judged silent.
 *
 * @typedef {"not_read"|"never"|"refused"|"silent"|"not_yet_this_boot"|"reporting"} HostState
 * @typedef {"alarm"|"notice"|"unknown"|"ok"} HostSeverity
 * @typedef {{name: string, group: string, label: string, fact: import("./facts.js").Fact,
 *            band: HostSeverity|null, suffix?: string, words?: string, brief?: string}} HostItem
 *   `words` is the item's incident wording when it differs from its fact's value (the
 *   throttling words, lower-cased mid-sentence); `brief` the Status chip's shorter one
 *   ("Host Management silent 3 min"). hostWords() reads them.
 * @typedef {{state: HostState, items: HostItem[],
 *            worst: {fact: import("./facts.js").Fact, severity: HostSeverity, item: HostItem}|null,
 *            severity: HostSeverity}} HostHealth
 * @typedef {{key: string, deviceId: string, name: string, frames: string[], playerHref: string,
 *            severity: "alarm"|"unknown", text: string}} HostIncident
 */

const HOST = LAYER_NAMES.host_core;

/** The firmware's throttle flags (§63) a Throttling item reads: its words when set. */
const FLAG = (when, word) => Object.freeze({ when, word });
const THROTTLE_FLAGS = Object.freeze({
  throttled_now: FLAG("now", "Throttled now"),
  under_voltage_now: FLAG("now", "Under-voltage now"),
  frequency_capped_now: FLAG("now", "Frequency capped now"),
  soft_temperature_limit_now: FLAG("now", "Soft temperature limit now"),
  under_voltage_occurred: FLAG("occurred", "under-voltage"),
  frequency_capped_occurred: FLAG("occurred", "frequency capping"),
  throttled_occurred: FLAG("occurred", "throttling"),
  soft_temperature_limit_occurred: FLAG("occurred", "soft temperature limit"),
});

/**
 * The cataloged host items. A metric item reads the one metric its key names: its label,
 * group, the unit Central's thresholds must name for a band to apply, its symbol and band
 * words (an item with no `bands`, like CPU, is never banded), and `format(value)`. The
 * Throttling item reads every name in its `flags`, each banded by its own served threshold,
 * and words them with `describe(rows)`.
 */
export const HOST_CATALOG = Object.freeze({
  soc_temperature: Object.freeze({
    label: "Temperature", group: "Thermal", unit: "celsius", symbol: "°C",
    bands: Object.freeze({ notice: "warm", alarm: "hot" }),
    format: (value) => `${value} °C`,
  }),
  throttling: Object.freeze({
    label: "Throttling", group: "Power and throttling", unit: "boolean", flags: THROTTLE_FLAGS,
    describe: (rows) => {
      const set = (when) => rows.filter((row) => row.value >= 1 && THROTTLE_FLAGS[row.name].when === when)
        .map((row) => THROTTLE_FLAGS[row.name].word);
      const now = set("now");
      if (now.length > 0) return now.join(" · ");
      const occurred = set("occurred");
      return occurred.length === 0 ? "None now"
        : `None now · ${occurred.join(", ")} occurred recently (the firmware's sticky ` +
          `${occurred.length === 1 ? "flag" : "flags"})`;
    },
  }),
  cpu_busy: Object.freeze({
    label: "CPU", group: "Compute", unit: "percent", format: (value) => `${value} % busy`,
  }),
  runtime_available: Object.freeze({
    label: "Storage", group: "Storage", unit: "bytes", format: (value) => `${gigabytes(value)} GB free in /run`,
  }),
  link_speed: Object.freeze({
    label: "Link speed", group: "Network", unit: "megabits_per_second", format: (value) => `${value} Mb/s`,
  }),
});

const APP_MANAGER = LAYER_NAMES.app_manager;
// App Manager's storage refusal (§63 "Storage short"): an item from G12's `preparation`, not a
// host metric, shown only while this boot's Host Management reports.
const PREPARATION = "preparation";
const PREPARATION_ENTRY = Object.freeze({ group: "Storage", label: "Storage" });

const RANK = Object.freeze({ ok: 0, unknown: 1, notice: 2, alarm: 3 });
// Why a box has no row (§62): absent from a read, or no read at all because it failed.
const NOT_READ = "not read";
const READ_FAILED = "the fleet host read failed";
const RECEIPT = "host";

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
/** "Throttled now · Under-voltage now" -> "throttled now · under-voltage now". */
const lowerFirst = (text) => text.split(" · ").map((word) => word.charAt(0).toLowerCase() + word.slice(1))
  .join(" · ");

/** A Host Management receipt as a `reported` (latest) fact: the one source of every age. */
function receipt(receivedAt, readAt) {
  return fact({ kind: "reported", source: HOST, receipt: "latest", receivedAt, readAt,
    field: "host.received_at" });
}

function item(name, fields) {
  const entry = name === RECEIPT ? { group: HOST, label: HOST }
    : name === PREPARATION ? PREPARATION_ENTRY : HOST_CATALOG[name];
  return Object.freeze({ name, group: entry.group, label: entry.label, band: null, ...fields });
}

/** Every item of a state in which nothing is known: the receipt and each metric, one why. */
function allUnknown(state, why) {
  const unknown = fact({ kind: "unknown", why });
  const items = [RECEIPT, ...Object.keys(HOST_CATALOG)].map((name) =>
    item(name, { fact: unknown, band: "unknown" }));
  return Object.freeze({ state, items, worst: { fact: unknown, severity: "unknown", item: items[0] },
    severity: "unknown" });
}

/**
 * The metric rows one catalog item reads from a sample, or why it cannot: each cataloged name
 * must appear exactly once with a number ("two values reported" wins over "not reported").
 */
function readRows(name, host) {
  const entry = HOST_CATALOG[name];
  const found = (entry.flags ? Object.keys(entry.flags) : [name]).map((metricName) =>
    (host.metrics ?? []).filter((metric) => metric?.name === metricName));
  if (found.some((rows) => rows.length > 1)) return { why: "two values reported" };
  if (found.some((rows) => rows.length === 0 || !isNumber(rows[0].value))) return { why: "not reported" };
  const rows = found.map(([metric]) => metric);
  return { rows, text: entry.flags ? entry.describe(rows) : entry.format(rows[0].value) };
}

/** Central's served threshold for one metric row, when it names the catalog's unit. */
function limitFor(metric, unit, thresholds) {
  if (metric.unit !== unit) return undefined;
  return (thresholds?.metrics ?? []).find((candidate) => candidate?.name === metric.name
    && candidate?.unit === unit);
}

/** One metric while reporting: its value, banded by Central's threshold when one applies. */
function reportedMetric(name, host, readAt, thresholds) {
  const entry = HOST_CATALOG[name];
  const read = readRows(name, host);
  if (read.why !== undefined) {
    return item(name, { fact: fact({ kind: "unknown", why: read.why }), band: "unknown" });
  }
  const reported = (band) => item(name, { band, fact: fact({ kind: "reported", source: HOST,
    receipt: "latest", value: read.text, receivedAt: host.received_at, readAt, field: "host.received_at" }) });
  if (entry.flags) {
    // Each flag is banded by its own served threshold; the item takes the worst.
    const limits = read.rows.map((metric) => [metric, limitFor(metric, entry.unit, thresholds)]);
    for (const band of ["alarm", "notice"]) {
      if (limits.some(([metric, limit]) => isNumber(limit?.[`${band}_at`]) && metric.value >= limit[`${band}_at`])) {
        return item(name, { band, words: lowerFirst(read.text), fact: fact({ kind: "derived", value: read.text,
          basis: "the firmware flag is set, Central's threshold" }) });
      }
    }
    return reported(limits.some(([, limit]) => limit !== undefined) ? "ok" : null);
  }
  if (entry.bands === undefined) return reported(null);
  const [metric] = read.rows;
  const limit = limitFor(metric, entry.unit, thresholds);
  for (const band of ["alarm", "notice"]) {
    const at = limit?.[`${band}_at`];
    if (isNumber(at) && metric.value >= at) {
      return item(name, { band, fact: fact({ kind: "derived", value: `${read.text} · ${entry.bands[band]}`,
        basis: `at or above ${at} ${entry.symbol}, Central's threshold` }) });
    }
  }
  return reported(limit === undefined ? null : "ok");
}

/** One metric as it was at this boot's last report, unbanded (silent, refused). */
function lastReportedMetric(name, host, readAt) {
  if (host === null) {
    return item(name, { fact: fact({ kind: "unknown", why: `no ${HOST} sample from this boot` }) });
  }
  const read = readRows(name, host);
  if (read.why !== undefined) return item(name, { fact: fact({ kind: "unknown", why: read.why }) });
  return item(name, { fact: fact({ kind: "reported", source: HOST, receipt: "latest",
    value: `${read.text} at last report`, receivedAt: host.received_at, readAt, field: "host.received_at" }) });
}

/**
 * App Manager's storage refusal while reporting (§62-§63): an alarm when the current boot's
 * newest App Manager sample is `refused` for `node_storage_capacity` with both numbers, else
 * no item (a newer sample clears it). While Central's App Manager intake for the box is full
 * today, the newest stored sample may be frozen, so it is not judged: the item is Unknown,
 * naming Central's refusal, neither an alarm nor a clear.
 */
function storageShort(preparation, intakeFull, readAt) {
  if (intakeFull) {
    return [item(PREPARATION, { band: "unknown", fact: fact({ kind: "unknown",
      why: `Central refused ${APP_MANAGER} reports today: its daily intake cap for this box is full` }) })];
  }
  if (preparation?.state !== "refused" || preparation.fault !== "node_storage_capacity"
      || !isNumber(preparation.required_bytes) || !isNumber(preparation.available_bytes)) {
    return [];
  }
  return [item(PREPARATION, { band: "alarm", fact: fact({ kind: "reported", source: APP_MANAGER,
    receipt: "latest", receivedAt: preparation.received_at, readAt, field: "preparation.received_at",
    value: `${APP_MANAGER} refused a preparation: needs ${gigabytes(preparation.required_bytes)} GB, ` +
      `room ${gigabytes(preparation.available_bytes)} GB` }) })];
}

function judged(state, items, fallback) {
  const ranked = items.filter((entry) => entry.band !== null && entry.band !== "ok"
    && (state !== "reporting" || entry.band !== "unknown"));
  const top = ranked.reduce((best, entry) => (best === null || RANK[entry.band] > RANK[best.band]
    ? entry : best), null);
  const severity = fallback ?? (top === null ? "ok" : top.band);
  return Object.freeze({ state, items: Object.freeze(items), severity,
    worst: top === null ? null : { fact: top.fact, severity: top.band, item: top } });
}

/**
 * Classify one box's fleet host row.
 *
 * @param {object|null} row `hostRow(read, deviceId)`
 * @param {object|null} read the served G12 document (`read_at`, `thresholds`)
 * @returns {HostHealth}
 */
export function classifyHost(row, read) {
  return classify(row, read, NOT_READ);
}

function classify(row, read, absent) {
  if (row == null) return allUnknown("not_read", absent);
  const readAt = read?.read_at ?? null;
  const thresholds = read?.thresholds ?? null;
  const limit = isNumber(thresholds?.host_silent_after_seconds) ? thresholds.host_silent_after_seconds : null;
  const host = row.host ?? null;
  const previous = isNumber(row.previous_boot_received_at) ? row.previous_boot_received_at : null;
  if (host === null && previous === null) {
    return allUnknown("never", `no ${HOST} report from this boot or the one before`);
  }
  const newest = receipt(host === null ? previous : host.received_at, readAt);
  const ago = newest.age === null ? null : `${formatAge(newest.age)} ago`;
  const whose = host === null ? `the previous boot's ${HOST} last reported` : "last reported";
  const metrics = Object.keys(HOST_CATALOG);
  if (limit !== null && newest.age !== null && newest.age > limit) {
    if (row.intake_full === true) {
      const refusal = fact({ kind: "derived", value: `Central refused ${HOST} reports today`,
        basis: "its daily intake cap for this box is full" });
      return judged("refused", [item(RECEIPT, { fact: refusal, band: "alarm", suffix: `last stored ${ago}` }),
        ...metrics.map((name) => lastReportedMetric(name, host, readAt))], "alarm");
    }
    const silence = fact({ kind: "derived", value: `${HOST} silent · ${whose} ${ago}`,
      basis: `no report for over ${limit} s, Central's limit` });
    return judged("silent", [item(RECEIPT, { fact: silence, band: "alarm",
      brief: `${HOST} silent ${formatAge(newest.age)}` }),
      ...metrics.map((name) => lastReportedMetric(name, host, readAt))], "alarm");
  }
  if (host === null) {
    const unknown = fact({ kind: "unknown",
      why: `on this boot · the previous boot's ${HOST} last reported ${ago ?? "at a time not served"}` });
    return judged("not_yet_this_boot", [RECEIPT, ...metrics].map((name) =>
      item(name, { fact: unknown, band: "unknown" })), "unknown");
  }
  return judged("reporting", [item(RECEIPT, { fact: newest }),
    ...metrics.map((name) => reportedMetric(name, host, readAt, thresholds)),
    ...storageShort(row.preparation ?? null, row.preparation_intake_full === true, readAt)]);
}

// The host facts record (G13) and the boot's base (§62): text a box's current boot carries.
const FACTS_LABEL = "Host facts";
const BASE_SOURCE = "this boot's node session";

/**
 * One box's host facts and base (console DDD §62), worded once per record: `receipt` is the
 * record's one line ("Host facts first received 3 d ago" through facts.js `receiptText`, or
 * why there is none), and each item's `reported` fact carries that same first receipt, so it
 * renders without its own (`FactLine receipt={false}`): "Host Management reported eth0 up".
 * The base is a `claimed` fact from the current admission's boot offer, never a host report.
 * Both come only from the current boot: G12 serves no other boot's facts.
 * The facts take the classifier's state, so one gate words a box's values and its facts: on
 * `silent` and `refused` each fact reads "… at last report", as its metrics do (§62, §66), and
 * never in the present tense of a box Central cannot hear.
 *
 * @param {object|null} row `hostRow(read, deviceId)`
 * @param {object|null} read the served G12 document (`read_at`)
 * @returns {{receipt: import("./facts.js").Fact,
 *            items: Array<{name: string, group: string, label: string, fact: import("./facts.js").Fact}>}}
 */
export function hostFactItems(row, read) {
  return factItems(row, read, NOT_READ, classify(row, read, NOT_READ));
}

// The states in which this boot's values are only its last report (§62's Silent row).
const LAST_REPORT_STATES = Object.freeze(new Set(["silent", "refused"]));

/** `health` is `classify(row, read, absent)`: the one gate both words read. */
function factItems(row, read, absent, health) {
  const lastReport = LAST_REPORT_STATES.has(health.state);
  const entry = (name, label, value) => Object.freeze({ name, group: label, label, fact: value });
  if (row == null) {
    const unknown = fact({ kind: "unknown", why: absent });
    return Object.freeze({ receipt: unknown, items: Object.freeze([entry("base", "Software", unknown)]) });
  }
  const boot = row.boot ?? null;
  const base = entry("base", "Software", boot === null
    ? fact({ kind: "unknown", why: "no current node boot admission" })
    : typeof boot.base_tag !== "string" || boot.base_tag === ""
      ? fact({ kind: "unknown", why: "this boot's offer names no base tag" })
      : fact({ kind: "claimed", value: `Base ${boot.base_tag}`, source: BASE_SOURCE }));
  const facts = row.facts ?? null;
  if (facts === null) {
    return Object.freeze({ receipt: fact({ kind: "unknown", why: "no host facts received on this boot" }),
      items: Object.freeze([base]) });
  }
  const readAt = read?.read_at ?? null;
  const reported = (value) => fact({ kind: "reported", source: HOST, receipt: "first",
    value: lastReport ? `${value} at last report` : value,
    receivedAt: facts.first_received_at, readAt, field: "facts.first_received_at" });
  const couldNot = (what) => fact({ kind: "unknown", why: `${HOST} could not read the ${what}` });
  const known = (value) => typeof value === "string" && value !== "";
  const items = [];
  if (!known(facts.interface)) {
    items.push(entry("link", "Network", couldNot("default-route interface")));
  } else if (!known(facts.link_state)) {
    items.push(entry("link", "Network", reported(facts.interface)),
      entry("link_state", "Network", couldNot(`link state of ${facts.interface}`)));
  } else {
    items.push(entry("link", "Network", reported(`${facts.interface} ${facts.link_state}`)));
  }
  items.push(entry("address", "Network",
    known(facts.address) ? reported(`address ${facts.address}`) : couldNot("address")));
  items.push(entry("kernel", "Software",
    known(facts.kernel_release) ? reported(`kernel ${facts.kernel_release}`) : couldNot("kernel release")));
  items.push(base);
  const receipt = fact({ kind: "reported", source: HOST, receipt: "first", value: FACTS_LABEL,
    receivedAt: facts.first_received_at, readAt, field: "facts.first_received_at" });
  return Object.freeze({ receipt, items: Object.freeze(items) });
}

/**
 * One box judged from the shell's fleet host read (§62): its row, `classifyHost` and
 * `hostFactItems`. The one way a page reads a box from `FleetHosts`, so the "Read failed"
 * wording cannot drift between pages: a failed read with no earlier result words every item
 * "Unknown: the fleet host read failed"; a read that does not list the box, "Unknown: not read".
 * A failed read with an earlier result judges those last good values (the page names the
 * failure beside them).
 *
 * @param {import("./fleetHosts.js").FleetHostsRead} hosts
 * @param {string} deviceId
 * @returns {{row: object|null, health: HostHealth, facts: ReturnType<typeof hostFactItems>}}
 */
export function judgeHost(hosts, deviceId) {
  const row = hostRow(hosts.read, deviceId);
  const absent = hosts.read == null && hosts.failed ? READ_FAILED : NOT_READ;
  const health = classify(row, hosts.read, absent);
  return Object.freeze({ row, health, facts: factItems(row, hosts.read, absent, health) });
}

/** "Frame lobby-left", "Frames a, b". */
function framesWord(frames) {
  return `${frames.length === 1 ? "Frame" : "Frames"} ${frames.join(", ")}`;
}

/**
 * One judged item in words, without its basis: an incident's text after the dash, and the
 * Status chip's text when `brief` (§62). An unknown fact keeps its "Unknown: " prefix.
 *
 * @param {HostItem} entry
 * @param {{brief?: boolean}} [options]
 * @returns {string}
 */
export function hostWords(entry, { brief = false } = {}) {
  if (brief && entry.brief !== undefined) return entry.brief;
  if (entry.words !== undefined) return entry.words;
  return entry.fact.kind === "unknown" ? factText(entry.fact) : entry.fact.value;
}

/**
 * The incidents of one box's classification (§62): exactly the classifier's alarm items, so
 * Needs attention, the Players table and the chip cannot disagree. Silent and Refused carry one
 * alarm item (the receipt), because the classifier bands nothing else outside `reporting`; a
 * Reporting row's alarms are its threshold items (a now flag, the temperature alarm, App
 * Manager's storage refusal). Never reported is the one Unknown incident.
 */
function incidentItems(health) {
  if (health.state === "never") return [health.items[0]];
  return health.items.filter((entry) => entry.band === "alarm");
}

/**
 * The host incidents Needs attention lists (§62): Bound Players only (an Unbound Player raises
 * none), from a read that did not fail. Each names the Player and its Frames: "pi-07 (Frame
 * lobby-left) — throttled now".
 *
 * @param {object|null} snapshot
 * @param {object|null} read the served G12 document; null when not read or the read failed
 * @param {{devices?: Map<string, object>}|null} [bootFacts] for the Players' names
 * @returns {HostIncident[]}
 */
export function hostIncidents(snapshot, read, bootFacts = null) {
  if (read == null) return [];
  const incidents = [];
  for (const row of playersByDevice(snapshot, bootFacts)) {
    if (row.standing !== "bound" || row.frames.length === 0) continue;
    const health = classifyHost(hostRow(read, row.deviceId), read);
    const frames = [...new Set(row.frames.map((entry) => entry.frameId))];
    for (const entry of incidentItems(health)) {
      incidents.push(Object.freeze({
        key: `player:${row.deviceId}:${entry.name}`, deviceId: row.deviceId, name: row.name, frames,
        playerHref: formatRoute({ section: "players", id: row.deviceId }),
        severity: entry.band === "alarm" ? "alarm" : "unknown",
        text: `${row.name} (${framesWord(frames)}) — ${hostWords(entry)}`,
      }));
    }
  }
  return incidents;
}

/**
 * The worst tier of a list of host incidents (§62): "alarm" if any is an alarm, "unknown" when
 * all are Unknown ("Never reported"), "ok" when there are none. The attention strip's colour
 * for its Player incidents, so an Unknown-only Player never reads as an alarm.
 *
 * @param {HostIncident[]} incidents
 * @returns {"alarm"|"unknown"|"ok"}
 */
export function incidentSeverity(incidents) {
  if (incidents.some((incident) => incident.severity === "alarm")) return "alarm";
  return incidents.length > 0 ? "unknown" : "ok";
}

/**
 * The Status facet's host chip (§62): the box's worst item ("pi-07 · throttled now", "pi-07 ·
 * Host Management silent 3 min"), else its receipt ("pi-07 · Host Management last reported 3 s
 * ago").
 *
 * @param {string} name the Player's name
 * @param {HostHealth} health `classifyHost(row, read)`
 * @returns {{text: string, severity: HostSeverity}}
 */
export function hostChip(name, health) {
  const words = health.worst === null ? factText(health.items[0].fact)
    : hostWords(health.worst.item, { brief: true });
  return Object.freeze({ text: `${name} · ${words}`, severity: health.severity });
}
