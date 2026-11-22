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
import { signInResultNotice } from "./domain/operator-access/access.ts";
import { CONSOLE_HEADER, onOriginRefused, writeCount } from "./session.js";

/**
 * @typedef {{inventory: object, runtime: object, media: object|null, at: number,
 *            readAt: number, playerReportsReadAt: number,
 *            readinessDiagnostics: object[], outputInterruptions: object[]}} Snapshot
 * @typedef {"checking"|"signedIn"|"signedOut"} Auth
 * @typedef {"rejected"|"expired"|"blocked"|"failed"|"signOutFailed"|"notAllowed"|
 *           "providerUnreachable"|"providerFailed"|null} AuthNotice
 * @typedef {{status: "loading"|"failed"|"read", method: "token"|"provider"|null,
 *            name: string|null}} SignInMethod
 *   How this Central signs a person in (decision 0035): with the deployment's operator token, or
 *   through its identity provider, whose name the sign-in button says. `method` is null until
 *   Central has answered once; a later read that fails keeps the last answer.
 */

// Sign in (POST), who am I (GET) and Log out (DELETE): pass A §5, decision 0035.
const SESSION_PATH = "/v1/operator/session";
// How to sign in: answered without a session.
const SIGN_IN_PATH = "/v1/operator/sign-in";

const METHOD_NOT_READ = Object.freeze({ status: "loading", method: null, name: null });

let signInResult;

/**
 * Why a sign-in through the provider did not finish: Central lands the browser back on the console
 * with `?sign_in=<code>`. Read ONCE per page load and removed from the address at once, so a reload
 * or a shared link never repeats it.
 *
 * @returns {AuthNotice}
 */
function takeSignInResult() {
  if (signInResult !== undefined) {
    return signInResult;
  }
  signInResult = null;
  try {
    const url = new URL(window.location.href);
    const code = url.searchParams.get("sign_in");
    if (code !== null) {
      signInResult = signInResultNotice(code);
      url.searchParams.delete("sign_in");
      window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    }
  } catch {
    // No address to read (not a browser page): no result.
  }
  return signInResult;
}

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
 * the whole subtree. `refresh` fetches Central's aggregate operator snapshot,
 * whose inventory, runtime, and media share one database read view, and replaces
 * Plane A wholesale. Accepted Player reports carry their own read timestamp.
 * It never merges into Plane B (useDraft).
 *
 * SIGN-IN (pass A §7). `auth` starts "checking": the first Plane A read decides
 * it (2xx signed in, 401 signed out; a network error or 5xx keeps checking and
 * the next poll retries). `signIn(token)` posts the token once — it is never
 * stored — and re-reads Plane A; `signOut()` deletes the session cookie,
 * clears Plane A and bumps `sessionEpoch`, on which the shell is keyed, so Log
 * out discards every draft. A Plane A 401 while signed in signs the tab out but
 * KEEPS the last snapshot: the sign-in screen overlays the still-mounted shell
 * and drafts survive signing in again (flow design §6 (a), overturning pass A
 * Question 4).
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
  const [authNotice, setAuthNotice] = useState(/** @type {AuthNotice} */ (takeSignInResult));
  // How Central signs a person in; this provider is its one reader.
  const [signInMethod, setSignInMethod] = useState(/** @type {SignInMethod} */ (METHOD_NOT_READ));
  const methodReadsRef = useRef(0);
  const sessionCheckRef = useRef(false);
  // Whether Central refused a write for its origin (apiWrite reports it).
  const [originRefused, setOriginRefused] = useState(false);
  // Whether the newest applied refresh failed (never inferred from age).
  const [refreshFailed, setRefreshFailed] = useState(false);
  // Bumped by each Log out; the shell is keyed on it (flow design §6 (b)).
  const [sessionEpoch, setSessionEpoch] = useState(0);
  // How many Plane A reads are in flight. `refreshing` (any in flight) is shown as
  // `aria-busy` on the snapshot status, so a refresh in progress is announced as
  // such and its end is observable: it clears only after the read has been applied
  // (or dropped), which is also when the poller's single-flight slot frees.
  const [inFlight, setInFlight] = useState(0);
  const issuedRef = useRef(0);
  const appliedRef = useRef(0);
  const pollingRef = useRef(false);

  const setAuth = useCallback((next) => {
    authRef.current = next;
    setAuthState(next);
  }, []);

  useEffect(() => onOriginRefused(() => setOriginRefused(true)), []);

  const read = useCallback(async () => {
    const ticket = ++issuedRef.current;
    const writesAtStart = writeCount();
    // Central reads inventory, runtime, and media under one database snapshot.
    // Keep its envelope intact until the whole request succeeds, then replace
    // Plane A in one state update. The local `at` is for UI staleness; `readAt`
    // and `playerReportsReadAt` retain Central's distinct timing facts.
    let next = null;
    let failure = null;
    try {
      const aggregate = await fetchJson("/v1/operator/snapshot");
      if (!aggregate?.inventory || !aggregate?.runtime || !aggregate?.media ||
          !Number.isFinite(aggregate.read_at) ||
          !Number.isFinite(aggregate.player_reports_read_at) ||
          !Array.isArray(aggregate.readiness_diagnostics) ||
          !Array.isArray(aggregate.output_interruptions)) {
        throw new Error("Invalid operator snapshot response");
      }
      next = {
        inventory: aggregate.inventory,
        runtime: aggregate.runtime,
        media: aggregate.media,
        at: Date.now(),
        readAt: aggregate.read_at,
        playerReportsReadAt: aggregate.player_reports_read_at,
        readinessDiagnostics: aggregate.readiness_diagnostics,
        outputInterruptions: aggregate.output_interruptions,
        // What the last planning pass could not do (central/planning_outcome.py): Needs
        // attention lists each Scene it refused.
        planning: aggregate.planning ?? { issues: [], since: null, dropped: 0 },
      };
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
      // rotated, cookie refused) signs the tab out, which pauses the poll and
      // shows the sign-in screen; a tab that was signed in says why. The last
      // snapshot is KEPT (flow design §6 (a)): the shell stays mounted under the
      // sign-in overlay, so no draft is lost and nothing reads as deleted. Only
      // Log out clears it. Any other failure (network/5xx) leaves the prior
      // snapshot and state for the next poll.
      if (failure?.status === 401) {
        const wasSignedIn = authRef.current === "signedIn";
        setAuth("signedOut");
        // A tab that was never signed in keeps its notice: why the provider's sign-in did not
        // finish (`?sign_in=`), which the first read finds still signed out.
        if (wasSignedIn) {
          setAuthNotice("expired");
        }
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

  const refresh = useCallback(async () => {
    setInFlight((count) => count + 1);
    try {
      return await read();
    } finally {
      setInFlight((count) => count - 1);
    }
  }, [read]);

  // A session exists again (a token accepted, or the provider's sign-in finished in another tab).
  const resume = useCallback(async () => {
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
  }, [refresh, setAuth]);

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
      await resume();
    },
    [resume],
  );

  // Read how Central signs a person in. The newest read wins; one that fails keeps the method
  // already known (it is the deployment's, and changes only when Central restarts).
  const readSignInMethod = useCallback(async () => {
    const ticket = ++methodReadsRef.current;
    setSignInMethod((previous) => (previous.method === null ? METHOD_NOT_READ : previous));
    let next = null;
    try {
      const answer = await fetchJson(SIGN_IN_PATH);
      if (answer?.method === "token") {
        next = { status: "read", method: "token", name: null };
      } else if (answer?.method === "provider" && typeof answer.name === "string" && answer.name !== "") {
        next = { status: "read", method: "provider", name: answer.name };
      }
    } catch {
      // Central did not answer: said below.
    }
    if (ticket !== methodReadsRef.current) {
      return;
    }
    setSignInMethod((previous) =>
      next ?? (previous.method === null ? { status: "failed", method: null, name: null } : previous));
  }, []);

  // While signed out through the provider with the shell still mounted, the sign-in itself happens
  // in another tab: ask Central whether this browser has a session again, and resume if so. The
  // sign-in screen calls this on its own cadence while it is shown (decision 0035).
  const checkSession = useCallback(async () => {
    if (authRef.current !== "signedOut" || sessionCheckRef.current) {
      return;
    }
    sessionCheckRef.current = true;
    try {
      await fetchJson(SESSION_PATH);
    } catch {
      // 401: still signed out. Anything else: Central did not answer; the next check asks again.
      return;
    } finally {
      sessionCheckRef.current = false;
    }
    if (authRef.current === "signedOut") {
      await resume();
    }
  }, [resume]);

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
    // A new session epoch: the shell is keyed on it, so it remounts and every
    // draft is discarded (flow design §6 (b)).
    setSessionEpoch((epoch) => epoch + 1);
    // Through a provider, Central answers the provider's own log-out address: going there ends the
    // provider's session too, and it comes back to the console's sign-in page (decision 0035).
    const endSession = result.data?.end_session;
    if (typeof endSession === "string" && endSession !== "") {
      window.location.assign(endSession);
    }
  }, [setAuth]);

  const dismissOriginRefused = useCallback(() => setOriginRefused(false), []);

  useEffect(() => {
    // The first Plane A read decides whether this browser is signed in. A failed
    // (non-401) initial load leaves Plane A null; the next poll retries.
    refresh().catch(() => {});
  }, [refresh]);

  const signedIn = auth === "signedIn";
  useEffect(() => {
    // On load, and again each time a signed-in tab is signed out: the deployment may have
    // changed how (setting a provider signs every browser out once).
    if (!signedIn) {
      readSignInMethod();
    }
  }, [signedIn, readSignInMethod]);

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
      signInMethod,
      readSignInMethod,
      checkSession,
      refreshFailed,
      refreshing: inFlight > 0,
      originRefused,
      dismissOriginRefused,
      sessionEpoch,
    }),
    [
      snapshot,
      refresh,
      auth,
      authNotice,
      signIn,
      signOut,
      signInMethod,
      readSignInMethod,
      checkSession,
      refreshFailed,
      inFlight,
      originRefused,
      dismissOriginRefused,
      sessionEpoch,
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
 * `authNotice`, `signIn(token)` and `signOut()`, `signInMethod` (how Central signs a person in:
 * the token, or its provider by name), `readSignInMethod()` (read it again) and `checkSession()`
 * (resume if this browser has a session again), `refreshFailed` (true while the
 * newest applied refresh failed), `refreshing` (a Plane A read is in flight),
 * `originRefused` (Central refused a write for its origin; `dismissOriginRefused`
 * clears it) and `sessionEpoch` (bumped
 * by each Log out; the shell is keyed on it). Must be used within a
 * SnapshotProvider so every region and useMutate share one Plane A.
 *
 * @returns {{snapshot: Snapshot|null, refresh: () => Promise<Snapshot|null>,
 *            auth: Auth, authNotice: AuthNotice,
 *            signIn: (token: string) => Promise<void>, signOut: () => Promise<void>,
 *            signInMethod: SignInMethod, readSignInMethod: () => Promise<void>,
 *            checkSession: () => Promise<void>,
 *            refreshFailed: boolean, originRefused: boolean,
 *            dismissOriginRefused: () => void, sessionEpoch: number}}
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
 * @typedef {"unknown"|"ok"|"degraded"|"unavailable"|"unreachable"} HealthState
 * @typedef {{status: "ok"|"failing", since: number|null, reason: string|null}} SchedulerStep
 * @typedef {{status: HealthState, reason: string|null, scheduler: string|null,
 *            steps: Record<string, SchedulerStep>}} CentralHealth
 */

// The scheduler states in which Players may be unable to report (the attention strip's one
// silent-Frames row): its loop is not ticking. A failing step ("degraded") is not one of them.
const STALLED = new Set(["stale", "stopped"]);

/**
 * Read Central's `/healthz` body (central/app.py `health`) into the pill's facts: the coarse
 * state ("degraded" when Central serves but a scheduler step fails), the reason it is not ok
 * ("database unavailable", "scheduler <status>", or the failing steps), the scheduler status
 * when its loop is stale or stopped (the attention strip's causal line reads it), and each
 * scheduler step's facet (the attention strip's "Planning is failing" row reads them).
 *
 * @param {boolean} httpOk
 * @param {any} body the parsed JSON body, or null
 * @returns {CentralHealth}
 */
export function readHealth(httpOk, body) {
  const schedulerStatus = body?.scheduler?.status ?? null;
  const scheduler = STALLED.has(schedulerStatus) ? String(schedulerStatus) : null;
  const steps = body?.scheduler?.steps ?? {};
  const status = body == null ? (httpOk ? "ok" : "unavailable")
    : body.status === "ok" ? "ok" : body.status === "degraded" ? "degraded" : "unavailable";
  let reason = null;
  if (status === "degraded") {
    const failing = Object.keys(steps).filter((name) => steps[name]?.status === "failing");
    reason = failing.length > 0 ? `${failing.join(", ")} failing` : "degraded";
  } else if (status !== "ok") {
    if (body?.database === false) {
      reason = "database unavailable";
    } else if (schedulerStatus !== null && schedulerStatus !== "ok") {
      reason = `scheduler ${String(schedulerStatus).replaceAll("_", " ")}`;
    }
  }
  return { status, reason, scheduler, steps };
}

/**
 * Health-pill poll (Bead 18; pass 2 §5). Polls the public, unauthenticated
 * `GET /healthz` every ~10s (design §9 cadence — matches the legacy health pill)
 * and reports Central's health, independent of Plane A and of the operator token:
 *  - status "ok"          — 200 with body status "ok"
 *  - status "degraded"    — 200 with body status "degraded": Central serves, a scheduler step
 *                           fails (`reason` names the failing steps)
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
    /** @type {CentralHealth} */ ({ status: "unknown", reason: null, scheduler: null, steps: {} }),
  );
  useEffect(() => {
    let live = true;
    // The same health keeps the same object, so a poll that reads no change re-renders
    // nothing that depends on it.
    const update = (next) =>
      setHealth((previous) =>
        previous.status === next.status &&
        previous.reason === next.reason &&
        previous.scheduler === next.scheduler &&
        JSON.stringify(previous.steps) === JSON.stringify(next.steps)
          ? previous
          : next,
      );
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
          update(readHealth(response.ok, body));
        }
      } catch {
        // No response at all (network error / timeout) — central is unreachable.
        if (live) {
          update({ status: "unreachable", reason: null, scheduler: null, steps: {} });
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
