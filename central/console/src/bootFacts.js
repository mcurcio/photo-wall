import { useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";

/**
 * Boot facts (slice 2 §5): the netboot record of each device, read ONCE at App
 * level and passed to the Players pages and the output chooser, so both show
 * the same serials and boot outcomes.
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
 * @typedef {{device_id: string, serial: string|null, known_good_tag: string|null,
 *            failed_tag: string|null, last_served_tag: string|null,
 *            boot_outcome: string|null}} DeviceRow
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
        const devices = new Map(result.data.devices.map((row) => [row.device_id, row]));
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
