import { fact, LAYER_NAMES } from "./facts.js";
import {
  ageAt, formatAge, NO_PANEL_AT_ENROLLMENT, outputStates, playerSerial, playersInOrder, playerStanding,
} from "./health.js";
import { frameForOutput } from "./join.js";
import { formatRoute } from "./routes.js";

/**
 * The Players list's read model (console DDD §9, Q1 = A: one home per box): one row per
 * physical box, keyed by its fleet DEVICE identity, because a box exists from its first
 * netboot. Built from the snapshot and the shell's boot facts only (bootFacts.js); it does
 * no node read.
 *
 * A Player in the snapshot is a row under its `device_id` (one Player per device: Central
 * re-associates a returning serial). A netboot device with no Player is a "Not enrolled"
 * row: a box Central saw at boot that never enrolled (U4). Retired Players stay, at their
 * standing; the netboot read lists active devices only, so a retired box never appears
 * twice.
 *
 * @typedef {"not-enrolled"|"unbound"|"bound"|"retired"} Standing
 * @typedef {{deviceId: string, player: object|null, standing: Standing,
 *            standingLabel: string, name: string, serial: string|null,
 *            frames: Array<{frameId: string, outputId: string}>}} PlayerRow
 */

/**
 * What stands in for a retired box's node and host values (console DDD §10): a plain
 * statement, not a `fact()` (the `unknown` kind would read "Unknown: …"). Central reads no
 * node or host report of a retired box (G12 omits it), so nothing is judged.
 */
/** The boot-records read failed: serials and boxes seen only at boot may be out of date. */
export const BOOT_FACTS_UNAVAILABLE = "Boot records unavailable";

export const RETIRED_NOT_READ = "Not read: Player retired";

// A name's handle is this many trailing characters of the serial (as health.js handles).
const HANDLE_LENGTH = 6;

/**
 * A box's name: "Player …<last six of its serial>" (the serial is the box's claim), or
 * "Player <device id>" when Central holds no serial for it.
 *
 * @param {string} deviceId
 * @param {string|null} serial
 * @returns {string}
 */
export function playerName(deviceId, serial) {
  return serial ? `Player …${serial.slice(-HANDLE_LENGTH)}` : `Player ${deviceId}`;
}

/**
 * The address of a Registry Player's home, its box's Player page (`#/players/<device-id>`),
 * or null when the Player is not in the snapshot. A link only: it reads nothing.
 *
 * @param {object|null} snapshot
 * @param {string} playerId
 * @returns {string|null}
 */
export function playerPageHref(snapshot, playerId) {
  const player = (snapshot?.inventory?.players ?? []).find((candidate) => candidate.id === playerId);
  return typeof player?.device_id === "string"
    ? formatRoute({ section: "players", id: player.device_id })
    : null;
}

/** The Frames bound to a Player's Outputs, by output id. */
function boundFrames(snapshot, playerId) {
  return (snapshot?.inventory?.outputs ?? [])
    .filter((output) => output.player_id === playerId)
    .map((output) => ({ output, frame: frameForOutput(snapshot, playerId, output.output_id) }))
    .filter(({ frame }) => frame !== null)
    .map(({ output, frame }) => ({ frameId: frame.id, outputId: output.output_id }))
    .sort((a, b) => (a.outputId < b.outputId ? -1 : a.outputId > b.outputId ? 1 : 0));
}

/**
 * Every box, one row each: enrolled Players in registration order, then boxes seen only at
 * boot, by device id.
 *
 * @param {object|null} snapshot
 * @param {{devices?: Map<string, object>}|null} bootFacts
 * @returns {PlayerRow[]}
 */
export function playersByDevice(snapshot, bootFacts) {
  const rows = [];
  const seen = new Set();
  for (const player of playersInOrder(snapshot)) {
    if (seen.has(player.device_id)) continue;
    seen.add(player.device_id);
    const standing = playerStanding(snapshot, player.id);
    const serial = playerSerial(snapshot, bootFacts, player.id);
    rows.push(Object.freeze({
      deviceId: player.device_id,
      player,
      standing: standing.state,
      standingLabel: standing.label,
      name: playerName(player.device_id, serial),
      serial,
      frames: boundFrames(snapshot, player.id),
    }));
  }
  const unenrolled = [...(bootFacts?.devices?.values() ?? [])]
    .filter((row) => typeof row?.device_id === "string" && !seen.has(row.device_id))
    .sort((a, b) => (a.device_id < b.device_id ? -1 : a.device_id > b.device_id ? 1 : 0));
  for (const row of unenrolled) {
    rows.push(Object.freeze({
      deviceId: row.device_id,
      player: null,
      standing: "not-enrolled",
      standingLabel: "Not enrolled · seen at boot, never enrolled",
      name: playerName(row.device_id, row.serial || null),
      serial: row.serial || null,
      frames: [],
    }));
  }
  return rows;
}

/**
 * The Panel on one Output as Central recorded it at the Player app's last enrollment, in ONE
 * wording for fleet and Wall (console DDD §19). Enrollment first marks every Output
 * `connected=false`, then writes the Outputs the Player app listed (`registry.py`), so
 * `connected=false` is Central's own record (`set`), not a report; only `connected=true`
 * comes from the Player app (`reported`, first receipt). Both may be stale.
 *
 * @param {object|null} observation the Output's served `observation`
 * @param {number|null} readAt the snapshot's `inventory.read_at`
 * @param {number|null} enrolledAt the Player's `last_seen` (Central's enrollment record)
 * @returns {import("./facts.js").Fact}
 */
export function panelAtEnrollment(observation, readAt, enrolledAt) {
  if (observation?.connected === true) {
    return fact({ kind: "reported", source: LAYER_NAMES.player_runtime, receipt: "first",
      value: "Panel connected at the Player app's last enrollment (may be stale)",
      receivedAt: enrolledAt, readAt, field: "last_seen" });
  }
  if (observation?.connected === false) {
    return fact({ kind: "set", value: NO_PANEL_AT_ENROLLMENT,
      receivedAt: enrolledAt, readAt });
  }
  return fact({ kind: "unknown", why: "Central holds no Panel record from the Player app's last enrollment" });
}

/**
 * Whether the console offers Identify Panel on one Output (console DDD §19), the one rule
 * both homes use: the Player page's Outputs and the Binding facet's picker. Central
 * identifies any connected, unbound Output of an active Player (`registry.py`
 * `identify_output`), whatever the Player's standing, so a Bound Player's free second Output
 * is offered. Whether the Player app offered the capability at enrollment is not served; a
 * refusal names it (equipmentApi.js `identifyOutput`).
 *
 * @param {object|null} snapshot
 * @param {string} playerId
 * @param {string} outputId
 * @returns {{offer: true}|{offer: false, reason: string}|{absent: true}} absent for a retired
 *   Player, or a Player or Output the snapshot does not list
 */
export function identifyOffer(snapshot, playerId, outputId) {
  const output = outputStates(snapshot, playerId).find((entry) => entry.outputId === outputId);
  switch (output?.state) {
    case "free":
      return { offer: true };
    case "no-display":
      return { offer: false, reason: "Connect a Panel and restart the Player app" };
    case "bound":
      return { offer: false, reason: "Central identifies only unbound Outputs" };
    default:
      return { absent: true };
  }
}

/**
 * When the Player app last enrolled, as Central recorded it (console DDD §19, R3): a `set`
 * fact, because `last_seen` is Central's enrollment record, not a report. The age is Central's
 * read time minus Central's record time.
 *
 * @param {object|null} player the Registry Player row
 * @param {number|null} readAt the snapshot's `inventory.read_at`
 * @returns {import("./facts.js").Fact}
 */
export function enrolledFact(player, readAt) {
  if (player == null) {
    return fact({ kind: "unknown", why: "the Player app has not enrolled" });
  }
  const age = ageAt(readAt, player.last_seen);
  if (Number.isNaN(age)) {
    return fact({ kind: "unknown", why: "Central's enrollment record time is not served" });
  }
  return fact({ kind: "set",
    value: `Player app enrolled ${formatAge(Math.max(0, age))} ago (authority epoch ${player.authority_epoch})` });
}
