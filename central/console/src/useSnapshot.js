import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { getToken, setToken, writeCount } from "./session.js";

/**
 * @typedef {{inventory: object, runtime: object, media: object|null, at: number}} Snapshot
 */

async function fetchJson(path) {
  const response = await fetch(path, {
    headers: {
      Authorization: "Bearer " + getToken(),
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

// Plane A is re-read on this cadence while the tab is visible (design pass 2 §7).
// It matches the calibration overtake poll it replaces.
const POLL_MS = 5000;

/**
 * Holds Plane A near the top of the tree and provides {snapshot, refresh} to
 * the whole subtree. `refresh` performs ONE atomic, timestamped fetch of
 * inventory + runtime (+ media) and replaces Plane A wholesale — inventory and
 * runtime are swapped together so a frame's binding row and its now-showing chip
 * always share one age (design §4a). It never merges into Plane B (useDraft).
 *
 * POLLING (pass 2 §7). The provider is the ONE poller: every 5 s while the tab
 * is visible it refreshes Plane A, reading the token fresh on each tick. A hidden
 * tab clears the interval; becoming visible refreshes at once and restarts it.
 * Polls are single-flight (the 15 s fetch timeout outlasts the interval); a
 * dropped or failed poll releases the slot, so the next tick still runs.
 *
 * NEWEST READ WINS. Every refresh — poll, mutation or manual — takes an
 * increasing ticket, and its outcome (success, 401 or any other failure) is
 * applied only if the ticket is newer than the last one applied. So a slow,
 * stale read can neither overwrite a newer snapshot nor, by failing late, log
 * the operator out or flag a failure the newer read disproved.
 *
 * WRITE FENCE. A refresh records the write counter (session.js; apiWrite moves
 * it) when it starts and is dropped if the counter moved before it returned: a
 * write that was in flight, or completed, while the read ran may not be
 * reflected in it. useMutate's own
 * refresh starts after the write's completion, so it is kept. The cost is bounded
 * starvation — back-to-back writes drop every overlapping poll.
 */
export function SnapshotProvider({ children }) {
  const [snapshot, setSnapshot] = useState(/** @type {Snapshot|null} */ (null));
  // Whether the last connect/refresh was refused for a bad operator token (a
  // 401 from any plane fetch). App renders the token form + a "not accepted"
  // message when this is set, instead of silently blanking (design R3).
  const [authRejected, setAuthRejected] = useState(false);
  // Whether the newest applied refresh failed (never inferred from age).
  const [refreshFailed, setRefreshFailed] = useState(false);
  const issuedRef = useRef(0);
  const appliedRef = useRef(0);
  const pollingRef = useRef(false);

  const refresh = useCallback(async () => {
    const ticket = ++issuedRef.current;
    const writesAtStart = writeCount();
    // Fetch every plane concurrently, then swap in ONE atomic snapshot; a
    // partial failure rejects and leaves the prior snapshot untouched. Media is
    // part of Plane A (design §4a) — inventory, runtime and the Source catalog
    // are swapped together so the Showrunner's Sources region and the wall's
    // now-showing chip always share one age.
    let next = null;
    let failure = null;
    try {
      const [inventory, runtime, media] = await Promise.all([
        fetchJson("/v1/operator/inventory"),
        fetchJson("/v1/operator/runtime"),
        fetchJson("/v1/operator/media"),
      ]);
      next = { inventory, runtime, media, at: Date.now() };
    } catch (error) {
      failure = error;
    }
    if (ticket <= appliedRef.current || writeCount() !== writesAtStart) {
      // Superseded by a newer read, or overlapped by a write: dropped whole.
      return null;
    }
    appliedRef.current = ticket;
    if (failure !== null) {
      setRefreshFailed(true);
      // A rejected operator token (401 on connect OR mid-session) drops the tab
      // back to the token-entry state: clear the IN-MEMORY token and the stale
      // snapshot, and flag the rejection so App re-renders the token form with a
      // "not accepted" message. Any other failure (network/5xx) leaves the prior
      // snapshot and token untouched for the next poll.
      if (failure?.status === 401) {
        setToken("");
        setSnapshot(null);
        setAuthRejected(true);
      }
      throw failure;
    }
    setSnapshot(next);
    // A successful load clears any prior token-rejection or failure notice.
    setAuthRejected(false);
    setRefreshFailed(false);
    return next;
  }, []);

  useEffect(() => {
    // Load the first snapshot on mount once a token is present. With no token
    // the shell stays in its empty state rather than firing a doomed
    // unauthenticated request.
    if (!getToken()) {
      return;
    }
    // A failed initial load leaves Plane A null; the next poll retries.
    refresh().catch(() => {});
  }, [refresh]);

  useEffect(() => {
    let id = null;
    const tick = () => {
      // The token is read fresh on every tick; with none in hand a read is
      // doomed, so it is skipped until Connect.
      if (!getToken() || pollingRef.current) {
        return;
      }
      pollingRef.current = true;
      refresh()
        .catch(() => {})
        .finally(() => {
          pollingRef.current = false;
        });
    };
    const start = () => {
      if (id === null) {
        id = setInterval(tick, POLL_MS);
      }
    };
    const stop = () => {
      if (id !== null) {
        clearInterval(id);
        id = null;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        tick();
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState === "visible") {
      start();
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);

  const value = useMemo(
    () => ({ snapshot, refresh, authRejected, refreshFailed }),
    [snapshot, refresh, authRejected, refreshFailed],
  );
  return React.createElement(SnapshotContext.Provider, { value }, children);
}

/**
 * Plane A read-snapshot hook (shared primitive #1).
 *
 * Returns the current atomic snapshot, a refresh fn that replaces Plane A
 * wholesale and NEVER merges into Plane B (it resolves null when its read was
 * superseded or fenced off by a write), `authRejected` (true once a fetch was
 * refused for a bad operator token) and `refreshFailed` (true while the newest
 * applied refresh failed). Must be used within a SnapshotProvider so every
 * region and useMutate share one Plane A.
 *
 * @returns {{snapshot: Snapshot|null, refresh: () => Promise<Snapshot|null>,
 *            authRejected: boolean, refreshFailed: boolean}}
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
 * @typedef {{status: HealthState, reason: string|null, scheduler: string|null}} CentralHealth
 */

/**
 * Read Central's `/healthz` body (central/app.py `health`) into the pill's facts:
 * the coarse state, the reason it is not ok ("database unavailable" or
 * "scheduler <status>"), and the scheduler status when it is neither "ok" nor
 * "disabled" (the attention strip's causal line reads it).
 *
 * @param {boolean} httpOk
 * @param {any} body the parsed JSON body, or null
 * @returns {CentralHealth}
 */
function readHealth(httpOk, body) {
  const schedulerStatus = body?.scheduler?.status ?? null;
  const scheduler =
    schedulerStatus === null || schedulerStatus === "ok" || schedulerStatus === "disabled"
      ? null
      : String(schedulerStatus);
  const healthy = body == null ? httpOk : body.status === "ok";
  const status = healthy ? "ok" : "unavailable";
  let reason = null;
  if (status !== "ok") {
    if (body?.database === false) {
      reason = "database unavailable";
    } else if (scheduler !== null) {
      reason = `scheduler ${scheduler.replaceAll("_", " ")}`;
    }
  }
  return { status, reason, scheduler };
}

/**
 * Health-pill poll (Bead 18; pass 2 §5). Polls the public, unauthenticated
 * `GET /healthz` every ~10s (design §9 cadence — matches the legacy health pill)
 * and reports Central's health, independent of Plane A and of the operator token:
 *  - status "ok"          — 200 with body status "ok"
 *  - status "unavailable" — a response that is not healthy (e.g. 503); `reason`
 *                           names the cause the body reports, when it does
 *  - status "unreachable" — the request failed / timed out (no response)
 *  - status "unknown"     — before the first poll returns
 * The pill lags by at most one interval (the stated cost, design §9).
 *
 * @param {number} [intervalMs]
 * @returns {CentralHealth}
 */
export function useHealth(intervalMs = 10000) {
  const [health, setHealth] = useState(
    /** @type {CentralHealth} */ ({ status: "unknown", reason: null, scheduler: null }),
  );
  useEffect(() => {
    let live = true;
    const poll = async () => {
      try {
        const response = await fetch("/healthz", {
          signal: AbortSignal.timeout(5000),
        });
        let body = null;
        try {
          body = await response.json();
        } catch {
          // A non-JSON body: fall back to the HTTP status alone.
        }
        if (live) {
          setHealth(readHealth(response.ok, body));
        }
      } catch {
        // No response at all (network error / timeout) — central is unreachable.
        if (live) {
          setHealth({ status: "unreachable", reason: null, scheduler: null });
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
