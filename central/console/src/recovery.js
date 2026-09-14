/**
 * Auto-recovery detection (Bead 9) — a pure diff over two Plane A snapshots.
 *
 * Recovery is INFERRED, never stored: the central store carries no "recovered"
 * flag. A known Pi that reboots re-enrolls by serial (device_id), which
 * reassociates the SAME player_id, preserves its Frame binding (is_bound stays
 * true), and bumps its `authority_epoch` (central/registry.py:128). The console
 * retains the prior snapshot's per-player `authority_epoch` and diffs it against
 * the current snapshot to recognise "a returning known Pi reappeared already
 * bound".
 *
 * Two suppressions are deliberate and load-bearing (design J1, §1a D-a):
 *  - TRUE FIRST RUN: with no prior snapshot there is nothing to diff, so nothing
 *    is a "return" — return []. (Surfacing recovery here would fire on every
 *    fresh operator load.)
 *  - PER-BOOT EPOCH BUMP ALONE: the epoch bumps on every boot, so an epoch bump
 *    is NOT itself recovery. Recovery is specifically a KNOWN serial (present in
 *    the prior snapshot) that reappears ALREADY BOUND with a bumped epoch — an
 *    unbound Pi rebooting, or a first-seen Pi, never raises the banner.
 *
 * @param {object|null} prevSnapshot the previously observed Plane A snapshot
 * @param {object|null} snapshot     the current Plane A snapshot
 * @returns {string[]} playerIds that returned already bound (empty on first run)
 */
export function detectRecovery(prevSnapshot, snapshot) {
  // True first run (or a torn read): nothing to diff -> no recovery.
  if (prevSnapshot == null || snapshot == null) {
    return [];
  }
  const prevEpoch = new Map(
    (prevSnapshot?.inventory?.players ?? []).map((player) => [
      player.id,
      player.authority_epoch,
    ]),
  );
  const recovered = [];
  for (const player of snapshot?.inventory?.players ?? []) {
    // Must reappear ALREADY BOUND (serial match, not a fresh operator bind).
    if (player.is_bound !== true) {
      continue;
    }
    // Must be a KNOWN serial: present in the prior snapshot. A first-seen Pi is
    // an arrival, not a return.
    if (!prevEpoch.has(player.id)) {
      continue;
    }
    // A bumped epoch is the returning-Pi signal (re-enroll by serial). An equal
    // epoch means the same still-running session, not a return.
    if (player.authority_epoch > prevEpoch.get(player.id)) {
      recovered.push(player.id);
    }
  }
  return recovered;
}
