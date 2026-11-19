import { useCallback } from "react";

import { apiWrite } from "./apiWrite.js";
import { fact, LAYER_NAMES } from "./facts.js";
import { formatAge, livenessFact } from "./health.js";
import { usePolledRead } from "./polledRead.js";

/**
 * The Player page's node read and its layer evidence (console DDD §9-§11).
 *
 * THE READ (`useNodeDevice`): one box's node records, read only while its Player page is
 * open — `GET …/node/devices/<id>` (its host line as Central keeps it, boot claims and reboot
 * records) — every `cadenceMs` through the shared
 * polled read (polledRead.js), never for a retired box (`skip`). Like the fleet host read it goes
 * through `apiWrite(path, {method: "GET"})`, so a failure never touches the session. A failed read
 * KEEPS the last values and reports its error beside them.
 *
 * THE MODEL (`layerEvidence`, `hostStart`): pure functions from that read and the snapshot to
 * facts (facts.js). A field that is null or absent renders
 * Unknown naming it; a payload of the wrong shape throws, which the section's error
 * boundary (SectionBoundary.jsx) contains. Every age is Central's read time minus
 * Central's receipt time (R10).
 *
 * @typedef {{code: string, status: number|null}} NodeReadError
 * @typedef {{read: object|null, readAt: number|null, error: NodeReadError|null}} NodeDevice
 *   `readAt` is the device read's `read_at` (Central's clock).
 * @typedef {{key: string, layer: string, level: string,
 *            facts: Array<{label: string, fact: import("./facts.js").Fact}>,
 *            details: string[]}} LayerRow
 */

export const NO_NODE_RECORD = "no current node record for this box";

const DEFAULT_CADENCE_MS = 5000;
const NOT_READ = Object.freeze({ read: null, readAt: null, error: null });

/**
 * The error a failed node read reports: its code, or the status, or "unanswered" (the
 * release read reports its failures the same way, releases.js).
 *
 * @param {{error: string|null, status: number}|null} result `apiWrite`'s answer, or null
 * @returns {NodeReadError}
 */
export function readError(result) {
  if (result === null) return { code: "unanswered", status: null };
  return { code: result.error ?? String(result.status), status: result.status };
}

/**
 * Read one box's node records while its Player page is open.
 *
 * @param {string} deviceId
 * @param {{cadenceMs?: number, skip?: boolean}} [options] `skip` (a retired box, an unknown
 *   one) reads nothing
 * @returns {NodeDevice & {refresh: () => Promise<void>, latest: () => NodeDevice}} `refresh`
 *   reads now (after a write); `latest` returns the newest read at call time, so a send
 *   judges what arrived even before React re-renders (fleetCommands.js `sendReboot`)
 */
export function useNodeDevice(deviceId, { cadenceMs = DEFAULT_CADENCE_MS, skip = false } = {}) {
  const base = `/v1/operator/node/devices/${encodeURIComponent(deviceId)}`;
  const load = useCallback(async (current) => {
    const read = await apiWrite(base, { method: "GET" }).catch(() => null);
    return read?.ok ? { read: read.data, readAt: read.data?.read_at ?? null, error: null }
      : { read: current.read, readAt: current.readAt, error: readError(read) };
  }, [base]);
  const { value, refresh, latest } = usePolledRead(load, { cadenceMs, skip, initial: NOT_READ });
  return { ...value, refresh, latest };
}

/** Why no node layer can be read, or null when the read can speak for them. */
export function nodeUnknown(nodeDevice) {
  if (nodeDevice?.read == null) {
    const code = nodeDevice?.error?.code;
    if (code === "node_device_unavailable") return NO_NODE_RECORD;
    return code ? `the node read failed (${code})` : "not read yet";
  }
  return null;
}


/**
 * The Host Management (L0) row: when Central last recorded a value of the host's line (its
 * metrics or facts) and the line's start, from the device read's `host` (decision 0034: the
 * host states its values only when they change, so this age is how long they have stood, never
 * silence; silence is Health's, from the fleet host read).
 */
function hostRow(nodeDevice) {
  const row = { key: "host", layer: LAYER_NAMES.host_core, level: "L0" };
  const why = nodeUnknown(nodeDevice) ?? (nodeDevice.read.host == null ? `no ${row.layer} line heard` : null);
  if (why !== null) {
    return { ...row, details: [], facts: [{ label: "Last reported", fact: fact({ kind: "unknown", why }) }] };
  }
  const { host, read_at: readAt } = nodeDevice.read;
  return { ...row,
    facts: [{ label: "Last reported", fact: fact({ kind: "reported", source: row.layer, receipt: "latest",
      receivedAt: host.received_at, readAt, field: "host.received_at" }) }],
    details: [`Line start ${host.epoch}`,
      `Source: ${row.layer}'s line as Central keeps it. It states a value only when it changes.`] };
}

/**
 * The Player page's layer rows, bottom up: Host Management (L0) and the Player app (L2). The host
 * row comes from the device read, the Player app's from the snapshot; each fact names its source.
 * Display Host states its Outputs on its own display line, never in the device read.
 *
 * @param {{nodeDevice: NodeDevice|null, snapshot: object|null, playerId: string|null}} input
 * @returns {LayerRow[]}
 */
export function layerEvidence({ nodeDevice, snapshot, playerId }) {
  return [hostRow(nodeDevice), playerAppRow(snapshot, playerId)];
}

/** The Player app (L2) row: its last readiness report on the current epoch (R3). */
function playerAppRow(snapshot, playerId) {
  const inventory = snapshot?.inventory;
  const player = (inventory?.players ?? []).find((candidate) => candidate.id === playerId) ?? null;
  const row = { key: "app", layer: LAYER_NAMES.player_runtime, level: "L2" };
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
    : livenessFact(player, inventory.read_at);
  const enrolled = typeof inventory?.read_at === "number" && typeof player.last_seen === "number"
    ? `Enrolled ${formatAge(Math.max(0, inventory.read_at - player.last_seen))} ago (enrollment is not a report)`
    : "Enrollment time not served";
  return { ...row, facts: [{ label: "Last reported", fact: reported }],
    details: [enrolled, `Authority epoch ${player.authority_epoch}`,
      "Source: readiness reports Central accepted (snapshot)."] };
}

/**
 * The start of the box's host line now, as Central keeps it: the line's stream creation its host's
 * birth was written in (a new one at every start of the Pi's bus, every boot among them), with
 * when Central recorded that birth. Never linked to a reboot request (a later start does not show
 * what caused it).
 *
 * @param {NodeDevice|null} nodeDevice
 * @returns {{epoch: string, fact: import("./facts.js").Fact}|{none: import("./facts.js").Fact}}
 */
export function hostStart(nodeDevice) {
  const why = nodeUnknown(nodeDevice);
  if (why !== null) return { none: fact({ kind: "unknown", why }) };
  const read = nodeDevice.read;
  const epoch = read.host?.epoch ?? null;
  if (typeof epoch !== "string") {
    return { none: fact({ kind: "set", value: `No ${LAYER_NAMES.host_core} line heard` }) };
  }
  return {
    epoch,
    fact: fact({ kind: "reported", value: `line start ${epoch}`, source: LAYER_NAMES.host_core, receipt: "first",
      receivedAt: read.host.born_at, readAt: read.read_at, field: "host.born_at" }),
  };
}
