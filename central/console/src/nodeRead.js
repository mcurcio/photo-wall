import { useCallback, useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { fact, words } from "./facts.js";
import { formatAge } from "./health.js";

/**
 * The Player page's node read and its layer evidence (console DDD §9-§11).
 *
 * THE READ (`useNodeDevice`): one box's node records, read only while its Player page is
 * open — `GET /v1/operator/node/status` (the effect gate), `GET …/node/devices/<id>` (its
 * sessions, host samples, preparation, broker projection, boot claims and reboot audit) and
 * `GET …/node/devices/<id>/app-attempts` (its app operations) — every `cadenceMs`, single
 * flight, paused while the tab is hidden, never for a retired box (`skip`). Like the boot
 * facts it goes through `apiWrite(path, {method: "GET"})`, so a failure never touches the
 * session. A failed read KEEPS the last values and reports its error beside them.
 *
 * THE MODEL (`layerEvidence`, `currentSessionBoot`, `processFacts`): pure functions from
 * that read and the snapshot to facts (facts.js). A field that is null or absent renders
 * Unknown naming it; a payload of the wrong shape throws, which the section's error
 * boundary (SectionBoundary.jsx) contains. Every age is Central's read time minus
 * Central's receipt time (R10).
 *
 * @typedef {{code: string, status: number|null}} NodeReadError
 * @typedef {{enabled: boolean|null, gate: object|null, read: object|null,
 *            operations: object|null, readAt: number|null,
 *            error: NodeReadError|null}} NodeDevice
 *   `enabled` is null until Central has answered, false when node management is off on this
 *   Central (no transport, or a read refused `node_control_disabled`). `readAt` is the
 *   device read's `read_at` (Central's clock).
 * @typedef {{key: string, layer: string, level: string,
 *            facts: Array<{label: string, fact: import("./facts.js").Fact}>,
 *            details: string[]}} LayerRow
 */

export const NODE_OFF = "node management is off on this Central";
export const NO_NODE_RECORD = "no current node record for this box";

const DEFAULT_CADENCE_MS = 5000;

/** The error a failed node read reports: its code, or the status, or "unanswered". */
function readError(result) {
  if (result === null) return { code: "unanswered", status: null };
  return { code: result.error ?? String(result.status), status: result.status };
}

/**
 * Read one box's node records while its Player page is open.
 *
 * @param {string} deviceId
 * @param {{cadenceMs?: number, skip?: boolean}} [options] `skip` (a retired box) reads nothing
 * @returns {NodeDevice & {refresh: () => Promise<void>}} `refresh` reads now (after a write)
 */
export function useNodeDevice(deviceId, { cadenceMs = DEFAULT_CADENCE_MS, skip = false } = {}) {
  const [state, setState] = useState(
    /** @type {NodeDevice} */ ({ enabled: null, gate: null, read: null, operations: null,
      readAt: null, error: null }),
  );
  const inFlight = useRef(false);
  const again = useRef(false);
  const alive = useRef(true);
  const base = `/v1/operator/node/devices/${encodeURIComponent(deviceId)}`;

  const readOnce = useCallback(async () => {
    const get = (path) => apiWrite(path, { method: "GET" }).catch(() => null);
    const [gate, read, operations] = await Promise.all([
      get("/v1/operator/node/status"), get(base), get(`${base}/app-attempts`),
    ]);
    if (!alive.current) return;
    const off = gate?.ok === true && gate.data?.transport_enabled === false
      || [read, operations].some((result) => result?.error === "node_control_disabled");
    const failed = [gate, read, operations].find((result) => result === null || !result.ok);
    setState((current) => ({
      enabled: off ? false : gate?.ok ? true : current.enabled,
      gate: gate?.ok ? gate.data : current.gate,
      read: read?.ok ? read.data : current.read,
      operations: operations?.ok ? operations.data : current.operations,
      readAt: read?.ok ? read.data?.read_at ?? null : current.readAt,
      error: failed === undefined ? null : readError(failed),
    }));
  }, [base]);

  // Single flight. A refresh asked for while a read is in flight is queued, not dropped:
  // that read may have started before the write it follows, so one more read runs after it.
  const refresh = useCallback(async () => {
    if (inFlight.current) {
      again.current = true;
      return;
    }
    inFlight.current = true;
    try {
      do {
        again.current = false;
        await readOnce();
      } while (again.current && alive.current);
    } finally {
      inFlight.current = false;
    }
  }, [readOnce]);

  useEffect(() => {
    alive.current = true;
    if (skip) return () => { alive.current = false; };
    const tick = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    tick();
    const timer = setInterval(tick, cadenceMs);
    document.addEventListener("visibilitychange", tick);
    return () => {
      alive.current = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [refresh, cadenceMs, skip]);

  return { ...state, refresh: skip ? noRefresh : refresh };
}

const noRefresh = async () => {};

/**
 * The App Effect Broker's app-process facts in one session's served `projection`: each
 * entry is a node snapshot message holding one fact (central/fleet/node_ingest.py), with
 * the sequence and Central's receipt time of the report that last changed it. Entries
 * that are not app-process facts, or do not decode, are left out; newest sequence first.
 *
 * @param {object|null} session one entry of the device read's `sessions`
 * @returns {Array<{pid: number, startTicks: number, invocationId: string|null,
 *                  appEpoch: number, environmentSha256: string, state: string,
 *                  sequence: number, receivedAt: number}>}
 */
export function processFacts(session) {
  const facts = [];
  for (const entry of session?.projection ?? []) {
    let item;
    try {
      item = JSON.parse(entry.payload)?.message?.facts?.[entry.fact_index];
    } catch {
      continue;
    }
    if (item?.type !== "AppProcessFact" || typeof item.process !== "object" || item.process === null) {
      continue;
    }
    facts.push({
      pid: item.process.pid,
      startTicks: item.process.start_ticks,
      invocationId: item.process.invocation_id?.uuid ?? null,
      appEpoch: item.app_epoch,
      environmentSha256: item.environment_sha256,
      state: item.state,
      sequence: entry.sequence,
      receivedAt: entry.received_at,
    });
  }
  return facts.sort((a, b) => b.sequence - a.sequence);
}

/** The current session of `owner` in a device read, or null. */
function currentSession(read, owner) {
  return (read?.sessions ?? []).find((session) => session.current && session.producer?.owner === owner)
    ?? null;
}

/**
 * The newest NON-current session of `owner` that holds this layer's evidence, or null. The
 * read serves sessions newest first (`issued_at DESC`) with each one's latest sample, so a
 * box whose host stopped reporting keeps its last receipt time after its session lapses.
 */
function lastSession(read, owner, hasEvidence) {
  return (read?.sessions ?? []).find((session) => !session.current && session.producer?.owner === owner
    && hasEvidence(session)) ?? null;
}

/** Why no node layer can be read, or null when the read can speak for them. */
export function nodeUnknown(nodeDevice) {
  if (nodeDevice?.enabled === false) return NODE_OFF;
  if (nodeDevice?.read == null) {
    const code = nodeDevice?.error?.code;
    if (code === "node_device_unavailable") return NO_NODE_RECORD;
    return code ? `the node read failed (${code})` : "not read yet";
  }
  return null;
}


/**
 * One node layer's row: its last-reported fact from its current session's field. With no
 * current session, the newest earlier session holding the layer's evidence (`hasEvidence`)
 * speaks instead, with a Session fact saying none is current; Unknown only when no session
 * of the layer has any.
 */
function nodeRow(key, layer, level, owner, nodeDevice, hasEvidence, build) {
  const why = nodeUnknown(nodeDevice);
  if (why !== null) {
    return { key, layer, level, facts: [{ label: "Last reported", fact: fact({ kind: "unknown", why }) }],
      details: [] };
  }
  const current = currentSession(nodeDevice.read, owner);
  const session = current ?? lastSession(nodeDevice.read, owner, hasEvidence);
  if (session === null) {
    return { key, layer, level, details: [],
      facts: [{ label: "Last reported", fact: fact({ kind: "unknown", why: `no current ${layer} session` }) }] };
  }
  const { facts, details } = build(session, nodeDevice.read.read_at);
  const lapsed = current === null
    ? [{ label: "Session", fact: fact({ kind: "set",
      value: `No current ${layer} session; the evidence above is from its last session` }) }]
    : [];
  return { key, layer, level, facts: [...facts, ...lapsed],
    details: [...details,
      `${current === null ? "Last session" : "Session"} ${session.session_id} · boot ${session.producer?.kernel_boot_id}`] };
}

/**
 * The Player page's five layer rows, bottom up: Host Management (L0), App Manager and App
 * Effect Broker (L1), Display Host (L1.5) and the Player app (L2). The node rows come from
 * the device read, the Player app's from the snapshot; each fact names its source.
 *
 * @param {{nodeDevice: NodeDevice|null, snapshot: object|null, playerId: string|null}} input
 * @returns {LayerRow[]}
 */
export function layerEvidence({ nodeDevice, snapshot, playerId }) {
  const host = nodeRow("host", "Host Management", "L0", "host_core", nodeDevice,
    (session) => session.host_observation != null, (session, readAt) => {
    const sample = session.host_observation?.sample;
    return {
      facts: [{ label: "Last reported", fact: fact({ kind: "reported", source: "Host Management",
        receipt: "latest", receivedAt: session.host_observation?.received_at, readAt,
        field: "host_observation.received_at" }) }],
      details: [
        ...(sample?.metrics ?? []).map((metric) =>
          `${words(metric.name)}: ${metric.value} ${metric.unit} (${words(metric.source)})`),
        ...(sample?.fault_code ? [`Reported fault: ${words(sample.fault_code)}`] : []),
        "Source: host samples (node device read). Host samples do not show visible pixels.",
      ],
    };
  });
  const manager = nodeRow("manager", "App Manager", "L1", "app_manager", nodeDevice,
    (session) => session.manager_preparation != null, (session, readAt) => {
    const preparation = session.manager_preparation;
    return {
      facts: [{ label: "Last reported", fact: fact({ kind: "reported", source: "App Manager",
        receipt: "latest", receivedAt: preparation?.received_at, readAt,
        value: preparation?.sample?.state ? `preparation ${words(preparation.sample.state)}` : null,
        field: "manager_preparation.received_at" }) }],
      details: [
        ...(preparation?.sample?.fault ? [`Fault: ${words(preparation.sample.fault)}`] : []),
        "Source: manager preparation samples. Preparation is not activation.",
      ],
    };
  });
  const broker = nodeRow("broker", "App Effect Broker", "L1", "app_effect_broker", nodeDevice,
    (session) => processFacts(session).length > 0, (session, readAt) => {
      const [latest, ...earlier] = processFacts(session);
      const process = latest === undefined
        ? fact({ kind: "unknown", why: "App Effect Broker has reported no app process on this session" })
        : fact({ kind: "reported", source: "App Effect Broker", receipt: "first",
          value: `the app ${words(latest.state)}`, receivedAt: latest.receivedAt, readAt,
          field: "projection received_at" });
      return {
        facts: [
          { label: "Last reported",
            fact: fact({ kind: "unknown", why: "Central does not serve when this layer last reported" }) },
          { label: "App process", fact: process },
        ],
        details: [
          ...[latest, ...earlier].filter(Boolean).map((item) =>
            `Process ${item.pid} (epoch ${item.appEpoch}, environment ${String(item.environmentSha256).slice(0, 12)}): ${words(item.state)}`),
          "Source: the broker's reported process facts. A running process is not visible output.",
        ],
      };
    });
  const offWhy = nodeUnknown(nodeDevice);
  const display = {
    key: "display", layer: "Display Host", level: "L1.5", details: [],
    facts: [{ label: "Output presentation", fact: fact({ kind: "unknown",
      why: offWhy ?? "Central does not hold Display Host's current presentation" }) }],
  };
  return [host, manager, broker, display, playerAppRow(snapshot, playerId)];
}

/** The Player app (L2) row: its last readiness report on the current epoch (R3). */
function playerAppRow(snapshot, playerId) {
  const inventory = snapshot?.inventory;
  const player = (inventory?.players ?? []).find((candidate) => candidate.id === playerId) ?? null;
  const row = { key: "app", layer: "Player app", level: "L2" };
  if (player === null) {
    return { ...row, details: [],
      facts: [{ label: "Last reported", fact: fact({ kind: "unknown", why: "this box has not enrolled" }) }] };
  }
  // Central reads no reports from a retired Player (its read excludes them), so a null
  // last_report_at there says nothing about the box.
  const reported = player.retired_at != null
    ? fact({ kind: "unknown", why: "Central does not read reports from a retired Player" })
    : player.last_report_at == null
    ? fact({ kind: "unknown",
      why: `no readiness report on the current enrollment (epoch ${player.authority_epoch})` })
    : fact({ kind: "reported", source: "Player app", receipt: "latest",
      receivedAt: player.last_report_at, readAt: inventory.read_at, field: "last_report_at" });
  const enrolled = typeof inventory?.read_at === "number" && typeof player.last_seen === "number"
    ? `Enrolled ${formatAge(Math.max(0, inventory.read_at - player.last_seen))} ago (enrollment is not a report)`
    : "Enrollment time not served";
  return { ...row, facts: [{ label: "Last reported", fact: reported }],
    details: [enrolled, `Authority epoch ${player.authority_epoch}`,
      "Source: readiness reports Central accepted (snapshot)."] };
}

/**
 * The boot of the box's current node session, as the box claimed it: the kernel boot id
 * every current session carries, with when Central first received that boot's claim.
 * Never linked to a reboot request (a later boot does not show what caused it).
 *
 * @param {NodeDevice|null} nodeDevice
 * @returns {{kernelBootId: string, fact: import("./facts.js").Fact}|{none: import("./facts.js").Fact}}
 */
export function currentSessionBoot(nodeDevice) {
  const why = nodeUnknown(nodeDevice);
  if (why !== null) return { none: fact({ kind: "unknown", why }) };
  const read = nodeDevice.read;
  const session = (read.sessions ?? []).find((candidate) => candidate.current) ?? null;
  const kernelBootId = session?.producer?.kernel_boot_id ?? null;
  if (kernelBootId === null) return { none: fact({ kind: "set", value: "No current node session" }) };
  const claim = (read.boot_claims ?? []).find((candidate) => candidate.kernel_boot_id === kernelBootId);
  return {
    kernelBootId,
    fact: fact({ kind: "claimed", value: `Boot ${kernelBootId}`, source: "the box", receipt: "first",
      receivedAt: claim?.first_received_at, readAt: read.read_at }),
  };
}
