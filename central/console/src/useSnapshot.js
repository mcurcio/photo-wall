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
    // Carry the HTTP status so a rejected operator token (401) can be told
    // apart from a transient network/500 failure by refresh() below.
    const error = new Error(path + " -> " + response.status);
    error.status = response.status;
    throw error;
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
  // Whether the last connect/refresh was refused for a bad operator token (a
  // 401 from any plane fetch). App renders the token form + a "not accepted"
  // message when this is set, instead of silently blanking (design R3).
  const [authRejected, setAuthRejected] = useState(false);

  const refresh = useCallback(async () => {
    // Fetch every plane concurrently, then swap in ONE atomic snapshot; a
    // partial failure rejects and leaves the prior snapshot untouched. Media is
    // part of Plane A (design §4a) — inventory, runtime and the Source catalog
    // are swapped together so the Showrunner's Sources region and the wall's
    // now-showing chip always share one age.
    try {
      const [inventory, runtime, media] = await Promise.all([
        fetchJson("/v1/operator/inventory"),
        fetchJson("/v1/operator/runtime"),
        fetchJson("/v1/operator/media"),
      ]);
      const next = { inventory, runtime, media, at: Date.now() };
      setSnapshot(next);
      // A successful load clears any prior token-rejection message.
      setAuthRejected(false);
      return next;
    } catch (error) {
      // A rejected operator token (401 on connect OR mid-session) drops the tab
      // back to the token-entry state: clear the IN-MEMORY token and the stale
      // snapshot, and flag the rejection so App re-renders the token form with a
      // "not accepted" message. Any other failure (network/5xx) leaves the prior
      // snapshot and token untouched for a later retry (Bead 18 richer surfacing).
      if (error?.status === 401) {
        setToken("");
        setSnapshot(null);
        setAuthRejected(true);
      }
      throw error;
    }
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

  useEffect(() => {
    // Bead 18: refresh Plane A when the operator returns to the tab. There is no
    // operator push channel (WS is player-only, design §9), so the console can
    // only be as fresh as its last read; refreshing on focus/visibility-change
    // narrows the staleness window whenever attention returns to the console. A
    // refresh with no token in hand is doomed, so it is skipped until Connect.
    const refreshOnReturn = () => {
      if (!adminToken) {
        return;
      }
      // A failed refresh leaves the prior snapshot untouched (refresh's own
      // catch); swallow here so an inactive-tab reject is never uncaught.
      refresh().catch(() => {});
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        refreshOnReturn();
      }
    };
    window.addEventListener("focus", refreshOnReturn);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", refreshOnReturn);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);

  const value = useMemo(
    () => ({ snapshot, refresh, authRejected }),
    [snapshot, refresh, authRejected],
  );
  return React.createElement(SnapshotContext.Provider, { value }, children);
}

/**
 * Plane A read-snapshot hook (shared primitive #1).
 *
 * Returns the current atomic snapshot, a refresh fn that replaces Plane A
 * wholesale and NEVER merges into Plane B, and `authRejected` (true once a fetch
 * was refused for a bad operator token). Must be used within a SnapshotProvider
 * so every region and useMutate share one Plane A.
 *
 * @returns {{snapshot: Snapshot|null, refresh: () => Promise<Snapshot>, authRejected: boolean}}
 */
export function useSnapshot() {
  const value = useContext(SnapshotContext);
  if (value === null) {
    throw new Error("useSnapshot must be used within a SnapshotProvider");
  }
  return value;
}

/**
 * Whole seconds since the current snapshot was read (pure). Drives the global
 * "updated N s ago" bar (Bead 18). Reads the snapshot's `at` timestamp — the one
 * age every region shares because inventory+runtime+media are swapped together
 * (design §4a) — so the clock is honest about staleness. Returns null when there
 * is no snapshot yet (never connected / initial load failed).
 *
 * @param {Snapshot|null} snapshot
 * @param {number} [now]
 * @returns {number|null}
 */
export function snapshotAge(snapshot, now = Date.now()) {
  if (snapshot == null || typeof snapshot.at !== "number") {
    return null;
  }
  return Math.max(0, Math.floor((now - snapshot.at) / 1000));
}

/**
 * Live snapshot-age clock (Bead 18). Reads Plane A from context and ticks once a
 * second so "updated N s ago" advances on its own; a refresh replaces Plane A
 * with a fresh `at`, so the age drops back to ~0 on the next render (Refresh
 * resets the clock). Returns null until the first snapshot lands.
 *
 * @returns {number|null}
 */
export function useSnapshotAge() {
  const { snapshot } = useSnapshot();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return snapshotAge(snapshot, now);
}

/**
 * @typedef {"unknown"|"ok"|"unavailable"|"unreachable"} HealthState
 */

/**
 * Health-pill poll (Bead 18). Polls the public, unauthenticated `GET /healthz`
 * every ~10s (design §9 cadence — matches the legacy health pill) and reports a
 * coarse reachability state, independent of Plane A and of the operator token.
 *  - "ok"          — 200 with body status "ok"
 *  - "unavailable" — a response that is not a healthy 200 (e.g. 503)
 *  - "unreachable" — the request failed / timed out (no response)
 *  - "unknown"     — before the first poll returns
 * The pill lags reachability by at most one interval (the stated cost, design §9).
 *
 * @param {number} [intervalMs]
 * @returns {HealthState}
 */
export function useHealth(intervalMs = 10000) {
  const [health, setHealth] = useState(/** @type {HealthState} */ ("unknown"));
  useEffect(() => {
    let live = true;
    const poll = async () => {
      try {
        const response = await fetch("/healthz", {
          signal: AbortSignal.timeout(5000),
        });
        let state = response.ok ? "ok" : "unavailable";
        try {
          const body = await response.json();
          state = body?.status === "ok" ? "ok" : "unavailable";
        } catch {
          // A non-JSON body: fall back to the HTTP status alone.
        }
        if (live) {
          setHealth(state);
        }
      } catch {
        // No response at all (network error / timeout) — central is unreachable.
        if (live) {
          setHealth("unreachable");
        }
      }
    };
    poll();
    const id = setInterval(poll, intervalMs);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return health;
}
