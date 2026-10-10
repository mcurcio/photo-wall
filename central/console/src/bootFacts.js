/**
 * Boot facts (slice 2 §5): each box's identity (`device_id` and the serial it claimed at boot),
 * taken from the shell's ONE fleet host read (fleetHosts.js, `GET /v1/operator/node/hosts`) and
 * passed to the Players pages and the output chooser, so both show the same serials. It is
 * derived on render and reads nothing of its own.
 *
 * `loaded` once a host read has arrived; `unavailable` when the latest host read failed, which
 * KEEPS the last known rows (the host read keeps its last read). With node control off there is
 * no host read: no rows, not loaded, and the node banner says why.
 *
 * Rows are keyed by `device_id`: a Player's `device_id` and the box's are both
 * `equipment_device_id("pi", serial)`. A box Central has no record of has no row. The serial is
 * the box's claim, not proof.
 *
 * @typedef {{device_id: string, serial: string|null}} DeviceRow
 * @typedef {{devices: Map<string, DeviceRow>, loaded: boolean, unavailable: boolean}} BootFacts
 */

const NONE = Object.freeze({ devices: new Map(), loaded: false, unavailable: false });

/**
 * @param {import("./fleetHosts.js").FleetHostsRead|null} hosts the shell's fleet host read
 * @returns {BootFacts}
 */
export function bootFactsFrom(hosts) {
  const rows = hosts?.read?.devices;
  if (!Array.isArray(rows)) {
    return hosts?.failed ? { ...NONE, unavailable: true } : NONE;
  }
  const devices = new Map(rows.map((row) =>
    [row.device_id, { device_id: row.device_id, serial: row.serial ?? null }]));
  return { devices, loaded: true, unavailable: hosts.failed === true };
}
