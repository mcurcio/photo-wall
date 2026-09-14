import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

/**
 * @typedef {{inventory: object, runtime: object, media: object|null, at: number}} Snapshot
 */

// The admin bearer token, held in memory only for this tab (never persisted) —
// the same discipline as the legacy operator page. Bead 0 ships no login UI, so
// the token defaults to empty and the shell renders its empty state; a later
// onboarding bead wires setToken() from a login control.
let adminToken = "";

/** Set the admin bearer token used by every Plane A / mutation fetch. */
export function setToken(token) {
  adminToken = token || "";
}

/** The admin bearer token currently held for this tab. */
export function getToken() {
  return adminToken;
}

async function fetchJson(path) {
  const response = await fetch(path, {
    headers: {
      Authorization: "Bearer " + adminToken,
      "Content-Type": "application/json",
    },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    throw new Error(path + " -> " + response.status);
  }
  return response.json();
}

const SnapshotContext = createContext(null);

/**
 * Holds Plane A near the top of the tree and provides {snapshot, refresh} to
 * the whole subtree. `refresh` performs ONE atomic, timestamped fetch of
 * inventory + runtime (+ media) and replaces Plane A wholesale — inventory and
 * runtime are swapped together so a frame's binding row and its now-showing chip
 * always share one age (design §4a). It never merges into Plane B (useDraft).
 */
export function SnapshotProvider({ children }) {
  const [snapshot, setSnapshot] = useState(/** @type {Snapshot|null} */ (null));

  const refresh = useCallback(async () => {
    // Fetch every plane concurrently, then swap in ONE atomic snapshot; a
    // partial failure rejects and leaves the prior snapshot untouched. Media is
    // part of Plane A (design §4a) — inventory, runtime and the Source catalog
    // are swapped together so the Showrunner's Sources region and the wall's
    // now-showing chip always share one age.
    const [inventory, runtime, media] = await Promise.all([
      fetchJson("/v1/operator/inventory"),
      fetchJson("/v1/operator/runtime"),
      fetchJson("/v1/operator/media"),
    ]);
    const next = { inventory, runtime, media, at: Date.now() };
    setSnapshot(next);
    return next;
  }, []);

  useEffect(() => {
    // Load the first snapshot on mount once a token is present. With no token
    // (Bead 0 has no login) the shell stays in its empty state rather than
    // firing a doomed unauthenticated request.
    if (!adminToken) {
      return;
    }
    let live = true;
    refresh().catch(() => {
      // A failed initial load leaves Plane A null; the shell renders empty and
      // a later refresh (focus/after-mutate, Bead 18) retries.
      if (!live) {
        return;
      }
    });
    return () => {
      live = false;
    };
  }, [refresh]);

  const value = useMemo(() => ({ snapshot, refresh }), [snapshot, refresh]);
  return React.createElement(SnapshotContext.Provider, { value }, children);
}

/**
 * Plane A read-snapshot hook (shared primitive #1).
 *
 * Returns the current atomic snapshot plus a refresh fn that replaces Plane A
 * wholesale and NEVER merges into Plane B. Must be used within a
 * SnapshotProvider so every region and useMutate share one Plane A.
 *
 * @returns {{snapshot: Snapshot|null, refresh: () => Promise<Snapshot>}}
 */
export function useSnapshot() {
  const value = useContext(SnapshotContext);
  if (value === null) {
    throw new Error("useSnapshot must be used within a SnapshotProvider");
  }
  return value;
}
