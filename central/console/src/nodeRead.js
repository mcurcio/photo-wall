import { useCallback } from "react";

import { apiWrite } from "./apiWrite.js";
import { fact, LAYER_NAMES, words } from "./facts.js";
import { formatAge, livenessFact } from "./health.js";
import { usePolledRead } from "./polledRead.js";

/**
 * The Player page's node read and its layer evidence (console DDD §9-§11).
 *
 * THE READ (`useNodeDevice`): one box's node records, read only while its Player page is
 * open — `GET …/node/devices/<id>` (its sessions, host samples, preparation, broker
 * projection, Display Host's newest exchange per Output, boot claims and reboot audit) and
 * `GET …/node/devices/<id>/app-attempts` (its app operations) — every `cadenceMs` through
 * the shared polled read (polledRead.js), never for a retired box and never while node
 * control is off (`skip`). The effect gate is NOT read here: the shell's node status read is
 * its one source (nodeControl.js). Like the fleet host read it goes through
 * `apiWrite(path, {method: "GET"})`, so a failure never touches the session. A failed read
 * KEEPS the last values and reports its error beside them; a read refused
 * `node_control_disabled` is such a failure, until the shell's read raises the banner.
 *
 * THE MODEL (`layerEvidence`, `currentSessionBoot`, `processFacts`): pure functions from
 * that read and the snapshot to facts (facts.js). A field that is null or absent renders
 * Unknown naming it; a payload of the wrong shape throws, which the section's error
 * boundary (SectionBoundary.jsx) contains. Every age is Central's read time minus
 * Central's receipt time (R10).
 *
 * @typedef {{code: string, status: number|null}} NodeReadError
 * @typedef {{read: object|null, operations: object|null, readAt: number|null,
 *            error: NodeReadError|null}} NodeDevice
 *   `readAt` is the device read's `read_at` (Central's clock).
 * @typedef {{key: string, layer: string, level: string,
 *            facts: Array<{label: string, fact: import("./facts.js").Fact}>,
 *            details: string[]}} LayerRow
 */

export const NO_NODE_RECORD = "no current node record for this box";

const DEFAULT_CADENCE_MS = 5000;
const NOT_READ = Object.freeze({ read: null, operations: null, readAt: null, error: null });

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
 * @param {{cadenceMs?: number, skip?: boolean}} [options] `skip` (a retired box, node control
 *   not on) reads nothing
 * @returns {NodeDevice & {refresh: () => Promise<void>, latest: () => NodeDevice}} `refresh`
 *   reads now (after a write); `latest` returns the newest read at call time, so a send
 *   judges what arrived even before React re-renders (fleetCommands.js `sendReboot`)
 */
export function useNodeDevice(deviceId, { cadenceMs = DEFAULT_CADENCE_MS, skip = false } = {}) {
  const base = `/v1/operator/node/devices/${encodeURIComponent(deviceId)}`;
  const load = useCallback(async (current) => {
    const get = (path) => apiWrite(path, { method: "GET" }).catch(() => null);
    const [read, operations] = await Promise.all([get(base), get(`${base}/app-attempts`)]);
    const failed = [read, operations].find((result) => result === null || !result.ok);
    return {
      read: read?.ok ? read.data : current.read,
      operations: operations?.ok ? operations.data : current.operations,
      readAt: read?.ok ? read.data?.read_at ?? null : current.readAt,
      error: failed === undefined ? null : readError(failed),
    };
  }, [base]);
  const { value, refresh, latest } = usePolledRead(load, { cadenceMs, skip, initial: NOT_READ });
  return { ...value, refresh, latest };
}

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
function nodeRow(key, level, owner, nodeDevice, hasEvidence, build) {
  const layer = LAYER_NAMES[owner];
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
  // Host Management's sample values live in Player › Health (HostHealthSection.jsx, §61);
  // its layer row keeps when it last reported and its session.
  const host = nodeRow("host", "L0", "host_core", nodeDevice,
    (session) => session.host_observation != null, (session, readAt) => ({
      facts: [{ label: "Last reported", fact: fact({ kind: "reported", source: LAYER_NAMES.host_core,
        receipt: "latest", receivedAt: session.host_observation?.received_at, readAt,
        field: "host_observation.received_at" }) }],
      details: [],
    }));
  const manager = nodeRow("manager", "L1", "app_manager", nodeDevice,
    (session) => session.manager_preparation != null, (session, readAt) => {
    const preparation = session.manager_preparation;
    return {
      facts: [{ label: "Last reported", fact: fact({ kind: "reported", source: LAYER_NAMES.app_manager,
        receipt: "latest", receivedAt: preparation?.received_at, readAt,
        value: preparation?.sample?.state ? `preparation ${words(preparation.sample.state)}` : null,
        field: "manager_preparation.received_at" }) }],
      details: [
        ...(preparation?.sample?.fault ? [`Fault: ${words(preparation.sample.fault)}`] : []),
        "Source: manager preparation samples. Preparation is not activation.",
      ],
    };
  });
  const broker = nodeRow("broker", "L1", "app_effect_broker", nodeDevice,
    (session) => processFacts(session).length > 0, (session, readAt) => {
      const [latest, ...earlier] = processFacts(session);
      const process = latest === undefined
        ? fact({ kind: "unknown", why: `${LAYER_NAMES.app_effect_broker} has reported no app process on this session` })
        : fact({ kind: "reported", source: LAYER_NAMES.app_effect_broker, receipt: "first",
          value: `the app ${words(latest.state)}`, receivedAt: latest.receivedAt, readAt,
          field: "projection received_at" });
      return {
        facts: [
          { label: "Last reported", fact: fact({ kind: "unknown", why: BROKER_SILENT }) },
          { label: "App process", fact: process },
        ],
        details: [
          ...[latest, ...earlier].filter(Boolean).map((item) =>
            `Process ${item.pid} (epoch ${item.appEpoch}, environment ${String(item.environmentSha256).slice(0, 12)}): ${words(item.state)}`),
          "Source: the broker's reported process facts. A running process is not visible output.",
        ],
      };
    });
  return [host, manager, broker, displayRow(nodeDevice), playerAppRow(snapshot, playerId)];
}

/** Why the App Effect Broker's last report is Unknown, served or not (§15). */
export const BROKER_SILENT =
  `${LAYER_NAMES.app_effect_broker} sends evidence only on change, and Central stores no receipt of its polls`;

/**
 * Display Host's newest exchange per Output on the box's current boot, as three `reported`
 * (latest) facts each, from the device read's `display_outputs` (§15, §16 display read). The
 * receipt age compares one producer's own boot clock with itself (R10) and is shown, never
 * judged. Nothing here is Panel pixels or names Display Host's own diagnostic page. An Output
 * Central served as `undecodable` (its stored exchange no longer parses) is one Unknown fact.
 *
 * @param {NodeDevice|null} nodeDevice
 * @param {number|null} readAt Central's read time
 * @returns {Array<{outputId: string, facts: import("./facts.js").Fact[]}>}
 */
export function displayOutputs(nodeDevice, readAt) {
  const source = LAYER_NAMES.display_host;
  return (nodeDevice?.read?.display_outputs ?? []).map((output) => {
    if (output.undecodable === true) {
      return { outputId: output.output_id, facts: [fact({ kind: "unknown",
        why: `Central could not decode ${source}'s last exchange for this Output` })] };
    }
    const reported = (value) => fact({ kind: "reported", source, receipt: "latest",
      value, receivedAt: output.received_at, readAt, field: "display_outputs received_at" });
    const surface = output.surface;
    const receipt = output.receipt;
    return {
      outputId: output.output_id,
      facts: [
        reported(typeof output.connected === "boolean"
          ? `Panel connector: ${output.connected ? "connected" : "not connected"}` : null),
        reported(surface == null ? `${source} reported no app surface admitted`
          : `Admitted surface: the app's surface for Frame ${surface.frame_id} (binding generation ${surface.binding_generation})`),
        reported(receipt?.matches_surface === true && typeof receipt.age_ms === "number"
          ? `Compositor receipt for that surface, sampled ${formatAge(Math.max(0, receipt.age_ms) / 1000)} before this report`
          : "No compositor receipt for that surface in this report"),
      ],
    };
  });
}

/**
 * The Display Host (L1.5) row: when it last reported (the newest exchange receipt across this
 * boot's Display Host producers), then each Output's three facts.
 */
function displayRow(nodeDevice) {
  const row = { key: "display", layer: LAYER_NAMES.display_host, level: "L1.5" };
  const why = nodeUnknown(nodeDevice);
  const unknownRow = (reason) => ({ ...row, details: [],
    facts: [{ label: "Last reported", fact: fact({ kind: "unknown", why: reason }) }] });
  if (why !== null) return unknownRow(why);
  const read = nodeDevice.read;
  if (!Array.isArray(read.display_outputs)) return unknownRow("display_outputs not served");
  if (read.display_outputs.length === 0) {
    return unknownRow(`${row.layer} has reported no Output on this boot`);
  }
  const times = read.display_outputs.map((output) => output.received_at).filter(Number.isFinite);
  const outputs = displayOutputs(nodeDevice, read.read_at);
  return { ...row,
    facts: [
      { label: "Last reported", fact: fact({ kind: "reported", source: row.layer, receipt: "latest",
        receivedAt: times.length > 0 ? Math.max(...times) : null, readAt: read.read_at,
        field: "display_outputs received_at" }) },
      ...outputs.flatMap((output) => output.facts.map((value) => ({ label: `Output ${output.outputId}`,
        fact: value }))),
    ],
    details: [
      ...read.display_outputs.filter((output) => output.surface != null).map((output) =>
        `Output ${output.output_id}: admitted surface configuration revision ${output.surface.config_revision}`),
      `Source: ${row.layer}'s newest display exchange per Output on the current boot (node device read). `
        + "A compositor receipt is not proof of Panel pixels.",
    ],
  };
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
