import { useCallback, useMemo } from "react";

import { apiWrite } from "./apiWrite.js";
import { readError } from "./nodeRead.js";
import { usePolledRead } from "./polledRead.js";

/**
 * The fleet host read (console DDD §63 G12): ONE read of `GET /v1/operator/node/hosts`, every
 * 15 s while the tab is visible, mounted once in the Shell and skipped until the shell's node
 * status read says node control is on (Shell.jsx says why that is stricter than
 * `nodeReadsAllowed`). It serves each active box's current-boot Host
 * Management sample, the previous boot's receipt, whether Central's observation intake for the
 * box is full today, Central's read time and Central's numbers-only thresholds. hostHealth.js
 * `classifyHost` is the one judge of it, and hostHealth.js `hostRow` finds one box in it
 * (a pure lookup kept out of this module, which reaches the write primitive, so the
 * classifier and the attention list import no write module: G1).
 *
 * Like the other node reads it goes through `apiWrite(path, {method: "GET"})`, so a failure
 * never touches the session. A failed read KEEPS the last read and names its error beside it;
 * consumers raise no host incident from a failed read (hostHealth.js `hostIncidents`).
 *
 * @typedef {{read: object|null, failed: boolean,
 *            error: import("./nodeRead.js").NodeReadError|null}} FleetHostsRead
 * @typedef {FleetHostsRead & {refresh: () => Promise<void>}} FleetHosts
 */

const CADENCE_MS = 15000;
const NOT_READ = Object.freeze({ read: null, failed: false, error: null });

/**
 * @param {{skip?: boolean}} [options] `skip` reads nothing (signed out, node reads not allowed)
 * @returns {FleetHosts}
 */
export function useFleetHosts({ skip = false } = {}) {
  const load = useCallback(async (current) => {
    const result = await apiWrite("/v1/operator/node/hosts", { method: "GET" }).catch(() => null);
    if (result?.ok) return Object.freeze({ read: result.data, failed: false, error: null });
    return Object.freeze({ read: current.read, failed: true, error: readError(result) });
  }, []);
  const { value, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, skip, initial: NOT_READ });
  return useMemo(() => ({ ...value, refresh }), [value, refresh]);
}
