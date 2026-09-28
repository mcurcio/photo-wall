import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { apiWrite } from "./apiWrite.js";
import { CONSOLE_HEADER, onOriginRefused, writeCount } from "./session.js";

/**
 * @typedef {{inventory: object, runtime: object, media: object|null, at: number}} Snapshot
 * @typedef {"checking"|"signedIn"|"signedOut"} Auth
 * @typedef {"rejected"|"expired"|"blocked"|"failed"|"signOutFailed"|null} AuthNotice
 */

// Sign in (POST) and Log out (DELETE): pass A §5.
const SESSION_PATH = "/v1/operator/session";

async function fetchJson(path) {
  // The session cookie travels with this same-origin fetch; no credential is held here.
  const response = await fetch(path, {
    headers: {
      ...CONSOLE_HEADER,
      "Content-Type": "application/json",
    },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    // Carry the HTTP status so a missing or ended session (401) can be told
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
 * SIGN-IN (pass A §7). `auth` starts "checking": the first Plane A read decides
 * it (2xx signed in, 401 signed out; a network error or 5xx keeps checking and
 * the next poll retries). `signIn(token)` posts the token once — it is never
 * stored — and re-reads Plane A; `signOut()` deletes the session cookie and
 * clears Plane A. A Plane A 401 while signed in signs the tab out. Drafts live in
 * the regions Plane A mounts, so they are lost with it (pass A Question 4).
 *
 * POLLING (pass 2 §7). The provider is the ONE poller: every 5 s while the tab
 * is visible and not signed out it refreshes Plane A. A hidden
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
  // The sign-in state (a ref mirrors it for the poller) and why the tab was
  // signed out or a sign-in failed; App renders the sign-in screen and the
  // notice instead of silently blanking (design R3).
  const [auth, setAuthState] = useState(/** @type {Auth} */ ("checking"));
  const authRef = useRef(/** @type {Auth} */ ("checking"));
  const [authNotice, setAuthNotice] = useState(/** @type {AuthNotice} */ (null));
  // Whether Central refused a write for its origin (apiWrite reports it).
  const [originRefused, setOriginRefused] = useState(false);
  // Whether the newest applied refresh failed (never inferred from age).
  const [refreshFailed, setRefreshFailed] = useState(false);
  const issuedRef = useRef(0);
  const appliedRef = useRef(0);
  const pollingRef = useRef(false);

  const setAuth = useCallback((next) => {
    authRef.current = next;
    setAuthState(next);
  }, []);

  useEffect(() => onOriginRefused(() => setOriginRefused(true)), []);

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
      // No session (401 on the first read OR mid-session: expired, token
      // rotated, cookie refused) drops the tab to the sign-in screen and clears
      // the stale snapshot; a tab that was signed in says why. Any other failure
      // (network/5xx) leaves the prior snapshot and state for the next poll.
      if (failure?.status === 401) {
        const wasSignedIn = authRef.current === "signedIn";
        setSnapshot(null);
        setAuth("signedOut");
        setAuthNotice(wasSignedIn ? "expired" : null);
      }
      throw failure;
    }
    setSnapshot(next);
    // A successful load signs the tab in and clears any prior notice.
    setAuth("signedIn");
    setAuthNotice(null);
    setRefreshFailed(false);
    return next;
  }, [setAuth]);

  const signIn = useCallback(
    async (token) => {
      let result;
      try {
        result = await apiWrite(SESSION_PATH, { method: "POST", body: { token } });
      } catch {
        setAuthNotice("failed");
        return;
      }
      if (!result.ok) {
        // A 403 is explained by the origin notice apiWrite raised.
        setAuthNotice(result.status === 401 ? "rejected" : result.status === 403 ? null : "failed");
        return;
      }
      setAuthNotice(null);
      // SigningIn -> Checking on 204 (pass A §7): the cookie is set, so the poller resumes
      // and a first read that fails (network/5xx) is retried, not left on the sign-in form.
      setAuth("checking");
      try {
        await refresh();
      } catch (error) {
        // Central issued the cookie but the browser did not send it back.
        if (error?.status === 401) {
          setAuthNotice("blocked");
        }
      }
    },
    [refresh, setAuth],
  );

  const signOut = useCallback(async () => {
    let result = null;
    try {
      result = await apiWrite(SESSION_PATH, { method: "DELETE" });
    } catch {
      // Central did not answer: the cookie may still be set, so stay signed in.
    }
    if (result === null || !result.ok) {
      setAuthNotice("signOutFailed");
      return;
    }
    // Any read in flight overlapped this write and is dropped by the fence.
    setSnapshot(null);
    setAuth("signedOut");
    setAuthNotice(null);
    setRefreshFailed(false);
  }, [setAuth]);

  const dismissOriginRefused = useCallback(() => setOriginRefused(false), []);

  useEffect(() => {
    // The first Plane A read decides whether this browser is signed in. A failed
    // (non-401) initial load leaves Plane A null; the next poll retries.
    refresh().catch(() => {});
  }, [refresh]);

  useEffect(() => {
    let id = null;
    const tick = () => {
      // Signed out, a read is doomed, so it is skipped until Sign in.
      if (authRef.current === "signedOut" || pollingRef.current) {
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
    () => ({
      snapshot,
      refresh,
      auth,
      authNotice,
      signIn,
      signOut,
      refreshFailed,
      originRefused,
      dismissOriginRefused,
    }),
    [
      snapshot,
      refresh,
      auth,
      authNotice,
      signIn,
      signOut,
      refreshFailed,
      originRefused,
      dismissOriginRefused,
    ],
  );
  return React.createElement(SnapshotContext.Provider, { value }, children);
}

/**
 * Plane A read-snapshot hook (shared primitive #1).
 *
 * Returns the current atomic snapshot, a refresh fn that replaces Plane A
 * wholesale and NEVER merges into Plane B (it resolves null when its read was
 * superseded or fenced off by a write), the sign-in state `auth` with its
 * `authNotice`, `signIn(token)` and `signOut()`, `refreshFailed` (true while the
 * newest applied refresh failed) and `originRefused` (Central refused a write
 * for its origin; `dismissOriginRefused` clears it). Must be used within a
 * SnapshotProvider so every region and useMutate share one Plane A.
 *
 * @returns {{snapshot: Snapshot|null, refresh: () => Promise<Snapshot|null>,
 *            auth: Auth, authNotice: AuthNotice,
 *            signIn: (token: string) => Promise<void>, signOut: () => Promise<void>,
 *            refreshFailed: boolean, originRefused: boolean,
 *            dismissOriginRefused: () => void}}
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
