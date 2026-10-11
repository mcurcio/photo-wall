import { fact, factText, LAYER_NAMES } from "./facts.js";
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
 * @typedef {{key: string, deviceId: string, name: string, frames: string[], hardwareHref: string,
 *            severity: "alarm"|"unknown", text: string}} HostIncident
 */

const HOST = LAYER_NAMES.host_core;

/**
 * One box's entry in a fleet host read, or null when the read does not list it (not read, or
 * a box Central omits: retired or revoked). Pure, and here rather than in fleetHosts.js (the
 * polling hook, which reaches the write primitive), so the classifier's import closure holds
 * models only (G1, enforced by tests/test_console_routes_r4.py).
 *
 * @param {object|null} read the served G12 document
 * @param {string} deviceId
 * @returns {object|null}
 */
export function hostRow(read, deviceId) {
  return (read?.devices ?? []).find((device) => device?.device_id === deviceId) ?? null;
}


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

// The base boot stages in boot order (contracts/node_host_facts.py BOOT_STAGES) and the unit
// that runs each (appliance/systemd): bound to the contracts by tests/test_console_host_health.py.
// No row cap is applied here: every reported failed unit and every non-zero kill is shown.
export const BOOT_STAGES = Object.freeze(["handoff", "storage", "prepare"]);
export const STAGE_UNITS = Object.freeze(Object.fromEntries(BOOT_STAGES.map((stage) =>
  [stage, `photo-wall-node-${stage}.service`])));
const STOPPED = Object.freeze(new Set(["refused", "failed"]));
const OOM_PREFIX = "oom_kill:";
const MEMCG = "memcg_present";

/** The boot report of the host facts record (G12 `facts.boot`), or null when not reported. */
function bootReport(facts) {
  const boot = facts?.boot;
  return boot !== null && typeof boot === "object" ? boot : null;
}

/**
 * The base stages that stopped, in boot order, each as `{stage, record}` (`record` absent when
 * the stage wrote none). A stage stops when its record says refused or failed, or when PID1
 * lists its own unit failed while its record is `running` or absent: the process was killed
 * before its exit write (an out-of-memory victim, TimeoutStartSec's SIGTERM) or the write
 * failed (docs/node-4gb-memory-design.md §6). Both boot items read this one rule. When the
 * failed units were not read (`failed_units` null, errata E-T3-2) only the records decide: a
 * `running` record stays running, neither stopped nor cleared.
 */
function stoppedStages(boot) {
  const stages = Array.isArray(boot.stages) ? boot.stages : [];
  const units = Array.isArray(boot.failed_units) ? new Set(boot.failed_units) : null;
  return BOOT_STAGES.flatMap((name) => {
    const record = stages.find((stage) => stage?.stage === name);
    const unitFailed = units !== null && units.has(STAGE_UNITS[name])
      && (record === undefined || record.state === "running");
    return STOPPED.has(record?.state) || unitFailed ? [{ stage: name, record }] : [];
  });
}

/**
 * Boot preparation (docs/node-4gb-memory-design.md §4.3 T4, §6): the first stage in boot order that stopped,
 * because the cause precedes its effects; else the first running; else done once `prepare` is
 * done; else not started. A stop is an alarm.
 */
function readBootPreparation(boot) {
  const stages = Array.isArray(boot.stages) ? boot.stages : [];
  const ordered = BOOT_STAGES.map((name) => stages.find((stage) => stage?.stage === name))
    .filter((stage) => stage !== undefined);
  const [first] = stoppedStages(boot);
  if (first !== undefined && !STOPPED.has(first.record?.state)) {
    return { band: "alarm", text: `boot preparation failed at ${first.stage} (unit failed, no exit record)` };
  }
  if (first !== undefined) {
    const stopped = first.record;
    const at = `boot preparation ${stopped.state} at ${stopped.stage}`;
    const numbers = isNumber(stopped.required_bytes) && isNumber(stopped.room_bytes);
    if (stopped.state === "refused" && numbers) {
      const needs = gigabytes(stopped.required_bytes);
      const room = gigabytes(stopped.room_bytes);
      return { band: "alarm", text: stopped.fault === "node_memory_class"
        ? `${at}: needs ${needs} GB of memory, the box has ${room} GB`
        : `${at}: needs ${needs} GB, room ${room} GB` };
    }
    return { band: "alarm", text: typeof stopped.fault === "string" ? `${at} (${stopped.fault})` : at };
  }
  const running = ordered.find((stage) => stage.state === "running");
  if (running !== undefined) return { band: null, text: `boot preparation running: ${running.stage}` };
  const prepared = ordered.some((stage) => stage.stage === "prepare" && stage.state === "done");
  return { band: null, text: prepared ? "boot preparation done" : "boot preparation not started" };
}

/**
 * Base units: PID1's failed photo-wall units, less the unit of each stage boot preparation
 * reads as stopped (its stop is that item's incident, so one cause raises one). A stage unit
 * whose stage did not stop stays listed. Any is an alarm. Units Host Management could not read
 * (`failed_units` null) read Unknown with no band: never "no base unit failed".
 */
function readBaseUnits(boot) {
  if (!Array.isArray(boot.failed_units)) return { why: "failed units not read", band: null };
  const repeated = new Set(stoppedStages(boot).map(({ stage }) => STAGE_UNITS[stage]));
  const names = boot.failed_units.filter((name) => typeof name === "string" && !repeated.has(name));
  const more = isNumber(boot.failed_units_more) && boot.failed_units_more > 0 ? boot.failed_units_more : 0;
  if (names.length === 0 && more === 0) return { band: null, text: "no base unit failed on this boot" };
  const listed = names.length === 0 ? `${more} not named`
    : `${names.join(" · ")}${more > 0 ? ` and ${more} more` : ""}`;
  return { band: "alarm",
    text: `base ${names.length + more === 1 ? "unit" : "units"} failed on this boot: ${listed}` };
}

/** Out-of-memory kills per slice (`oom_kill:<slice>`), most first; any kill is a notice. */
function readOutOfMemory(rows) {
  const kills = rows.map((row) => [row.name.slice(OOM_PREFIX.length), row.value])
    .filter(([, value]) => value > 0)
    .sort(([a, x], [b, y]) => (y - x) || (a < b ? -1 : a > b ? 1 : 0));
  return kills.length === 0 ? { band: null, text: "No out-of-memory kills on this boot" }
    : { band: "notice", text: `Out-of-memory kills on this boot: ${kills.map(([slice, value]) =>
      `${slice} ${value}`).join(" · ")}` };
}

/**
 * Memory limits: the kernel's memory controller (`memcg_present`). Without it no slice's
 * MemoryMax= is enforced; the base still mounts its store (report-only, errata E-FX2-1), so
 * the absence is a notice (a warning, never an alarm or an incident).
 */
function readMemoryLimits([row]) {
  return row.value >= 1 ? { band: null, text: "memory controller on: memory limits enforced" }
    : { band: "notice", text: "memory controller absent: memory limits not enforced" };
}

/** Whether a metric name belongs to a family key: exact, or `<prefix><suffix>` for a prefix. */
const inFamily = (name, family) => (family.endsWith(":")
  ? name.startsWith(family) && name.length > family.length : name === family);

/**
 * The cataloged host items. A metric item reads the metric families its `families` name (an
 * exact name, or a prefix ending in ":" that covers every `<prefix><suffix>` row): its label,
 * group, the unit Central's thresholds must name for a band to apply, its symbol and band
 * words (an item with no `bands`, like CPU, is never banded), and `format(value)`. The
 * Throttling item reads every name in its `flags`, each banded by its own served threshold,
 * and words them with `describe(rows)`. An item with `judge` bands itself: from the metric
 * rows of its families (`judge(rows)`), or, with `boot: true` and no families, from the
 * host facts record's boot report (`judge(boot)`), carrying that record's receipt.
 *
 * `families` is the item-to-family map: with NOT_SHOWN it covers every family of
 * contracts/node_observation.py METRIC_FAMILIES (tests/test_console_host_health.py).
 */
export const HOST_CATALOG = Object.freeze({
  soc_temperature: Object.freeze({
    label: "Temperature", group: "Thermal", unit: "celsius", symbol: "°C", families: ["soc_temperature"],
    bands: Object.freeze({ notice: "warm", alarm: "hot" }),
    format: (value) => `${value} °C`,
  }),
  throttling: Object.freeze({
    label: "Throttling", group: "Power and throttling", unit: "boolean", flags: THROTTLE_FLAGS,
    families: Object.keys(THROTTLE_FLAGS),
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
    label: "CPU", group: "Compute", unit: "percent", families: ["cpu_busy"], format: (value) => `${value} % busy`,
  }),
  runtime_available: Object.freeze({
    label: "Storage", group: "Storage", unit: "bytes", families: ["runtime_available"],
    format: (value) => `${gigabytes(value)} GB free in /run`,
  }),
  out_of_memory: Object.freeze({
    label: "Out of memory", group: "Storage", families: [OOM_PREFIX], judge: readOutOfMemory,
  }),
  memory_limits: Object.freeze({
    label: "Memory limits", group: "Storage", families: [MEMCG], judge: readMemoryLimits,
  }),
  link_speed: Object.freeze({
    label: "Link speed", group: "Network", unit: "megabits_per_second", families: ["link_speed"],
    format: (value) => `${value} Mb/s`,
  }),
  boot_preparation: Object.freeze({
    label: "Boot preparation", group: "Software", families: [], boot: true, judge: readBootPreparation,
  }),
  base_units: Object.freeze({
    label: "Base units", group: "Software", families: [], boot: true, judge: readBaseUnits,
  }),
});

/**
 * The metric families HostCore posts that no console item shows (contracts/node_observation.py
 * METRIC_FAMILIES): they reach Central and read raw on the Hardware Pi page's Health "Every reported
 * metric" and in G12. `metrics_dropped` is appended by `valid_metrics` alone.
 */
export const NOT_SHOWN = Object.freeze([
  "uptime", "load_1m", "memory_total", "memory_available", "cma_total", "cma_free",
  "memory_peak:", "manager_summary_known", "manager_running", "manager_attempts",
  "manager_recovery_required", "manager_start_unknown", "manager_summary_age",
  "local_recovery_active", "local_recovery_reboot", "metrics_dropped",
]);

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
  if (entry.judge !== undefined) {
    // Every row of its families (an exact name, or each `<prefix><suffix>`), each name once,
    // at least one.
    const rows = (host.metrics ?? []).filter((metric) => typeof metric?.name === "string"
      && entry.families.some((family) => inFamily(metric.name, family)) && isNumber(metric.value));
    if (new Set(rows.map((metric) => metric.name)).size !== rows.length) return { why: "two values reported" };
    if (rows.length === 0) return { why: "not reported" };
    return { rows, ...entry.judge(rows) };
  }
  const found = entry.families.map((metricName) =>
    (host.metrics ?? []).filter((metric) => metric?.name === metricName));
  if (found.some((rows) => rows.length > 1)) return { why: "two values reported" };
  if (found.some((rows) => rows.length === 0 || !isNumber(rows[0].value))) return { why: "not reported" };
  const rows = found.map(([metric]) => metric);
  return { rows, text: entry.flags ? entry.describe(rows) : entry.format(rows[0].value) };
}

/**
 * One item's reading from a box's current boot, with the receipt it carries: a metric item's
 * from this boot's newest sample (`latest`), a boot item's from the host facts record
 * (`first`, its record's receipt, renewed by any changed value). An older node that sends no
 * boot report reads "not reported", which raises no tier while reporting (§62 Tier).
 */
function readItem(name, host, facts) {
  const entry = HOST_CATALOG[name];
  if (entry.boot === true) {
    const boot = bootReport(facts);
    if (boot === null) return { why: "not reported" };
    return { ...entry.judge(boot), receipt: { receipt: "first", receivedAt: facts.first_received_at,
      field: "facts.first_received_at" } };
  }
  return { ...readRows(name, host), receipt: { receipt: "latest", receivedAt: host.received_at,
    field: "host.received_at" } };
}

/** Central's served threshold for one metric row, when it names the catalog's unit. */
function limitFor(metric, unit, thresholds) {
  if (metric.unit !== unit) return undefined;
  return (thresholds?.metrics ?? []).find((candidate) => candidate?.name === metric.name
    && candidate?.unit === unit);
}

/** One metric while reporting: its value, banded by Central's threshold when one applies. */
function reportedMetric(name, host, facts, readAt, thresholds) {
  const entry = HOST_CATALOG[name];
  const read = readItem(name, host, facts);
  if (read.why !== undefined) {
    // A judge may word its own Unknown with no band (`band: null`); otherwise Unknown is banded.
    return item(name, { fact: fact({ kind: "unknown", why: read.why }),
      band: read.band === null ? null : "unknown" });
  }
  const reported = (band) => item(name, { band, fact: fact({ kind: "reported", source: HOST,
    value: read.text, readAt, ...read.receipt }) });
  // A self-judging item's band is its own (a stopped boot stage, a failed unit, a kill), as
  // App Manager's storage refusal's is: no Central threshold is read for it.
  if (entry.judge !== undefined) return reported(read.band);
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
function lastReportedMetric(name, host, facts, readAt) {
  if (host === null) {
    return item(name, { fact: fact({ kind: "unknown", why: `no ${HOST} sample from this boot` }) });
  }
  const read = readItem(name, host, facts);
  if (read.why !== undefined) return item(name, { fact: fact({ kind: "unknown", why: read.why }) });
  return item(name, { fact: fact({ kind: "reported", source: HOST, value: `${read.text} at last report`,
    readAt, ...read.receipt }) });
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
  const facts = row.facts ?? null;
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
        ...metrics.map((name) => lastReportedMetric(name, host, facts, readAt))], "alarm");
    }
    const silence = fact({ kind: "derived", value: `${HOST} silent · ${whose} ${ago}`,
      basis: `no report for over ${limit} s, Central's limit` });
    return judged("silent", [item(RECEIPT, { fact: silence, band: "alarm",
      brief: `${HOST} silent ${formatAge(newest.age)}` }),
      ...metrics.map((name) => lastReportedMetric(name, host, facts, readAt))], "alarm");
  }
  if (host === null) {
    const unknown = fact({ kind: "unknown",
      why: `on this boot · the previous boot's ${HOST} last reported ${ago ?? "at a time not served"}` });
    return judged("not_yet_this_boot", [RECEIPT, ...metrics].map((name) =>
      item(name, { fact: unknown, band: "unknown" })), "unknown");
  }
  return judged("reporting", [item(RECEIPT, { fact: newest }),
    ...metrics.map((name) => reportedMetric(name, host, facts, readAt, thresholds)),
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
 *
 * The base has two sources, never merged (owner decision 2026-10-02): `base_reported` is Host
 * Management's report of the base this boot runs (the facts record's `base_tag`, `reported`),
 * and `base` is Central's offer, the tag of the boot offer the current admission claimed
 * (`claimed`, labelled "Central's offer"). When both name a tag and the tags differ,
 * `base_mismatch` is a `derived` fact naming both; it carries no band (no alarm is defined).
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
  const known = (value) => typeof value === "string" && value !== "";
  const boot = row.boot ?? null;
  const offered = known(boot?.base_tag) ? boot.base_tag : null;
  const base = entry("base", "Software", boot === null
    ? fact({ kind: "unknown", why: "no current node boot admission" })
    : offered === null
      ? fact({ kind: "unknown", why: "this boot's offer names no base tag" })
      : fact({ kind: "claimed", value: `Central's offer: base ${offered}`, source: BASE_SOURCE }));
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
  const reportedBase = known(facts.base_tag) ? facts.base_tag : null;
  items.push(entry("base_reported", "Software",
    reportedBase === null ? couldNot("base tag") : reported(`base ${reportedBase}`)));
  items.push(base);
  if (reportedBase !== null && offered !== null && reportedBase !== offered) {
    items.push(entry("base_mismatch", "Software", fact({ kind: "derived",
      value: `Base differs: ${HOST} reported ${reportedBase}, Central's offer ${offered}`,
      basis: "the reported tag and the offered tag differ" })));
  }
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

// The Hardware list's groups, in order (console DDD §52, §61, G2).
const GROUP_ORDER = Object.freeze({ bound: 0, spare: 1, retired: 2 });

/**
 * A spare's box (G2): read with Central's thresholds withheld, so no threshold judgement can
 * reach its words (no "· hot", no "silent", no "at last report", no "Central's threshold" or
 * "Central's limit" basis), then with every remaining band taken off. Its values read as
 * plain `reported` facts with their receipt age; a silent spare's silence is that age.
 */
function describeSpare(hosts, deviceId) {
  const read = hosts.read == null ? hosts.read : { ...hosts.read, thresholds: null };
  return unbanded(judgeHost({ ...hosts, read }, deviceId));
}

/** A box with every band taken off: its words unchanged, judged as nothing. */
function unbanded(judgedBox) {
  const items = (list) => Object.freeze(list.map((entry) => Object.freeze({ ...entry, band: null })));
  return Object.freeze({
    health: Object.freeze({ ...judgedBox.health, items: items(judgedBox.health.items), worst: null,
      severity: null }),
    facts: judgedBox.facts,
  });
}

/**
 * The Hardware list's rows, by group then name (console DDD §52, §61, rule G2): the ONE place a
 * box's standing decides whether its host values are judged. It judges; the list orders a
 * group worst first by the tiers it hands over (patterns/entity-list.tsx).
 *   bound    a Bound Player: judged (`judgeHost`), tiered by its severity. Only these rows are
 *            tiered, so "worst first" names a box in trouble.
 *   spare    an Unbound Player or a box seen at boot and never enrolled: its values read with
 *            Central's thresholds withheld (`describeSpare`), so no band, no tier and no
 *            threshold words (a spare is never alarmed, G2; its silence reads as a plain
 *            receipt age, §52), by name, below every Bound row.
 *   retired  a retired Player: not judged at all (Central reads none of its reports, and G12
 *            omits it), so no health and no facts; the page states RETIRED_NOT_READ. Last.
 * With `hosts` null (the fleet host read skipped) nothing is judged and only the groups order.
 *
 * @param {import("./players.js").PlayerRow[]} rows `playersByDevice(snapshot, bootFacts)`
 * @param {import("./fleetHosts.js").FleetHostsRead|null} hosts
 * @returns {Array<{row: import("./players.js").PlayerRow, group: "bound"|"spare"|"retired",
 *            tier: HostSeverity|null, health: HostHealth|null,
 *            facts: ReturnType<typeof hostFactItems>|null}>}
 */
export function playersTable(rows, hosts) {
  const byName = (a, b) => (a.row.name < b.row.name ? -1 : a.row.name > b.row.name ? 1 : 0);
  return rows.map((row) => {
    const group = row.standing === "retired" ? "retired" : row.standing === "bound" ? "bound" : "spare";
    if (group === "retired" || hosts === null) {
      return Object.freeze({ row, group, tier: null, health: null, facts: null });
    }
    if (group === "spare") return Object.freeze({ row, group, tier: null, ...describeSpare(hosts, row.deviceId) });
    const judgedBox = judgeHost(hosts, row.deviceId);
    return Object.freeze({ row, group, tier: judgedBox.health.severity, health: judgedBox.health,
      facts: judgedBox.facts });
  }).sort((a, b) => (GROUP_ORDER[a.group] - GROUP_ORDER[b.group]) || byName(a, b));
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
 * Needs attention, the Hardware list and the chip cannot disagree. Silent and Refused carry one
 * alarm item (the receipt), because the classifier bands nothing else outside `reporting`; a
 * Reporting row's alarms are its threshold items (a now flag, the temperature alarm, App
 * Manager's storage refusal) and its boot items (a stopped boot stage, a failed base unit).
 * Never reported is the one Unknown incident.
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
        hardwareHref: formatRoute({ section: "hardware", id: row.deviceId }),
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
 * The Frame page Overview's host chip (§62): the box's worst item ("pi-07 · throttled now", "pi-07 ·
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
