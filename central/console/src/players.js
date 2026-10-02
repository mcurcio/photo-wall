import { playerSerial, playersInOrder, playerStanding } from "./health.js";
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
