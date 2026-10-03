import { useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { fact } from "./facts.js";

/**
 * Boot facts (slice 2 §5): each device's identity from the shared `devices` table, read
 * ONCE at App level and passed to the Players pages and the output chooser, so both show
 * the same serials. Node boot writes that table too, so it is lane-neutral; the console
 * reads only `device_id` and `serial` from it (console DDD Part E §24).
 *
 * An OPTIONAL side read of `GET /v1/operator/netboot`, never part of the atomic
 * Plane A snapshot: its failure must not fail the wall. It goes through
 * `apiWrite(path, {method: "GET"})` (a GET does not move the write fence) and
 * never touches the session — a 401 here does not log the operator out.
 *
 * It re-reads on a new snapshot when 30 s have passed since the last read began,
 * or when the set of Player `device_id`s changed. It adds no timer, so it
 * inherits the poller's hidden-tab pause. Reads are single-flight; a trigger
 * missed while one is in flight is caught by the next snapshot. Any non-2xx or
 * thrown read sets `unavailable` and KEEPS the last known rows.
 *
 * Rows are keyed by `device_id`: a Player's `device_id` and the netboot
 * device's are both `equipment_device_id("pi", serial)`. A Pi that never
 * netbooted has no row. The serial is the device's claim, not proof.
 *
 * @typedef {{device_id: string, serial: string|null}} DeviceRow
 * @typedef {{devices: Map<string, DeviceRow>, loaded: boolean, unavailable: boolean}} BootFacts
 */

const REREAD_MS = 30000;

/** The set of Player device ids in a snapshot, as one comparable key. */
function deviceKey(snapshot) {
  return [...new Set((snapshot?.inventory?.players ?? []).map((player) => player.device_id))]
    .sort()
    .join("\n");
}

/**
 * @param {{at: number, inventory: object}|null} snapshot the current Plane A snapshot
 * @returns {BootFacts}
 */
export function useBootFacts(snapshot) {
  const [facts, setFacts] = useState(
    /** @type {BootFacts} */ ({ devices: new Map(), loaded: false, unavailable: false }),
  );
  const lastRef = useRef(/** @type {{at: number, key: string}|null} */ (null));
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (snapshot == null || inFlightRef.current) {
      return;
    }
    const key = deviceKey(snapshot);
    const last = lastRef.current;
    if (last !== null && snapshot.at - last.at < REREAD_MS && key === last.key) {
      return;
    }
    lastRef.current = { at: snapshot.at, key };
    inFlightRef.current = true;
    apiWrite("/v1/operator/netboot", { method: "GET" })
      .then((result) => {
        if (!result.ok || !Array.isArray(result.data?.devices)) {
          throw new Error(String(result.status));
        }
        const devices = new Map(result.data.devices.map((row) =>
          [row.device_id, { device_id: row.device_id, serial: row.serial ?? null }]));
        setFacts({ devices, loaded: true, unavailable: false });
      })
      .catch(() => {
        setFacts((current) => ({ ...current, unavailable: true }));
      })
      .finally(() => {
        inFlightRef.current = false;
      });
  }, [snapshot]);

  return facts;
}

// The deprecated boot paths, in words (G5 serves lane-neutral values).
const DEPRECATED_PATHS = Object.freeze({
  offer: "a deprecated boot offer",
  base_without_offer: "a base image served without an offer",
});

/**
 * The deprecated-path line (console DDD Part E §25-§26, G5): Central's newest boot record for
 * this box is not a node boot. Central compares its own clock readings and serves the result
 * (`deprecated_boot` on the node device read); the age is Central's read time minus that
 * record's time (R10). Null when Central serves none.
 *
 * @param {{path: string, recorded_at: number}|null|undefined} deprecatedBoot
 * @param {number|null} readAt the device read's `read_at`
 * @returns {import("./facts.js").Fact|null}
 */
export function deprecatedBootFact(deprecatedBoot, readAt) {
  if (deprecatedBoot == null) return null;
  const path = DEPRECATED_PATHS[deprecatedBoot.path] ?? `a boot record Central calls "${deprecatedBoot.path}"`;
  return fact({ kind: "set", value: `Central's newest boot record for this box is ${path}`,
    receivedAt: deprecatedBoot.recorded_at, readAt });
}
