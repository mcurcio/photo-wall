import React, { useEffect, useMemo, useRef, useState } from "react";

import "./index.css";
import { AttentionStrip } from "./AttentionStrip.jsx";
import { useBootFacts } from "./bootFacts.js";
import { EquipmentRoster } from "./EquipmentRoster.jsx";
import { Guidance } from "./Guidance.jsx";
import { facetFor, frameHealth } from "./health.js";
import { Inspector } from "./Inspector.jsx";
import { Plan } from "./Plan.jsx";
import { detectRecovery } from "./recovery.js";
import { Showrunner } from "./Showrunner.jsx";
import { UnplacedTray } from "./UnplacedTray.jsx";
import { useMode } from "./useMode.js";
import { setToken } from "./session.js";
import { useHealth, useSnapshot, useSnapshotAge } from "./useSnapshot.js";

// The Central pill's colour: the shared health severity for each /healthz state.
const PILL_SEVERITY = { ok: "ok", unavailable: "alarm", unreachable: "alarm" };

/**
 * The console app shell.
 *
 * Bead 0 built the empty frame + Plane A wiring. Bead 1 adds:
 *  - a minimal shared "Token" control (design §2): an "Operator token" input and
 *    a "Connect" button that set the in-memory token and trigger one Plane A
 *    refresh. The token is never persisted (useSnapshot holds it in memory only).
 *  - a Surface filter that groups frames by `surface_id` and switches plans,
 *    defaulting to the first Surface present.
 *  - the read-only per-Surface SVG Plan and the Unplaced tray.
 */
export default function App() {
  const { snapshot, refresh, authRejected, refreshFailed } = useSnapshot();
  // Top-level Wall/Showrunner mode (Plane B). A snapshot refresh replaces the
  // fetched inventory alone and never resets this (design §2).
  const { mode, setMode } = useMode();
  // Boot facts (slice 2 §5): ONE optional read of the netboot records, shared by
  // the Equipment roster and the output chooser.
  const bootFacts = useBootFacts(snapshot);
  // Bead 18: the global snapshot-age clock (advances each second, resets on
  // refresh) and the ~10s /healthz pill. Both are global, so they read one
  // age/one health regardless of Wall/Showrunner mode. The pill is the ONE place
  // Central's own health is shown (pass 2 §5).
  const age = useSnapshotAge();
  const health = useHealth();
  const centralHealth =
    health.status === "unavailable" ? health.reason ?? "unavailable" : health.status;
  const [tokenInput, setTokenInput] = useState("");
  const [surfaceId, setSurfaceId] = useState(/** @type {string|null} */ (null));
  const [selection, setSelection] = useState(/** @type {string|null} */ (null));
  // Which Inspector facet is open (Plane B, component-local). Defaults to
  // "commissioning"; selecting another Frame keeps it (pass 2 §4 — no reset),
  // and only attention-strip navigation moves it, to the facet showing the cause.
  const [facet, setFacet] = useState(/** @type {string} */ ("commissioning"));
  // A request for the Inspector to take focus, issued ONLY by attention-strip
  // navigation (a fresh number each time); plain tile or tray selection clears
  // it, so selecting a Frame never moves focus. The Inspector consumes it once
  // (onFocusDone), so a later remount does not refocus.
  const [focusRequest, setFocusRequest] = useState(/** @type {number|null} */ (null));
  const focusSeqRef = useRef(0);

  // Unplaced-tray drag-out (Bead 11). The dragged frame id lives in a REF so the
  // plan's pointer-up reads it synchronously (a full press->move->release can
  // fire before React re-renders — cf. Plan's own dragRef). A window-level
  // pointer-up clears it so a press that does NOT land on the plan (a plain tray
  // click, or a release anywhere else) cancels the drag rather than leaving a
  // stale id that a later plan release would wrongly consume. The plan's own
  // handler runs first (React binds at the root, below window in the bubble
  // path), so a genuine drop is read and cleared before this reset sees it.
  const trayDragRef = useRef(/** @type {string|null} */ (null));
  // The plan region: the focus successor of a delete from the plan or the tray.
  const planRegionRef = useRef(/** @type {HTMLElement|null} */ (null));
  useEffect(() => {
    const clear = () => {
      trayDragRef.current = null;
    };
    window.addEventListener("pointerup", clear);
    return () => window.removeEventListener("pointerup", clear);
  }, []);

  // Auto-recovery banner (design J1, §1a D-a). Recovery is INFERRED by diffing
  // the CURRENT Plane A snapshot against the PRIOR one, so App retains the prior
  // snapshot itself in a ref — useSnapshot's frozen {snapshot, refresh} shape is
  // untouched. The banner surfaces "a known Pi returned already bound"; it is
  // suppressed on the true first run (no prior snapshot) by detectRecovery.
  const prevSnapshotRef = useRef(/** @type {object|null} */ (null));
  const [recovered, setRecovered] = useState(/** @type {string[]} */ ([]));
  useEffect(() => {
    if (snapshot == null) {
      return;
    }
    const returned = detectRecovery(prevSnapshotRef.current, snapshot);
    if (returned.length > 0) {
      setRecovered(returned);
    }
    prevSnapshotRef.current = snapshot;
  }, [snapshot]);

  // Selecting a Frame opens its Inspector. Plain selection (plan or tray) keeps
  // the open facet and never moves focus; attention-strip navigation passes the
  // facet that shows the frame's cause (health.js facetFor) and asks the
  // Inspector to take focus.
  const selectFrame = (frameId, nextFacet = null) => {
    setSelection(frameId);
    if (nextFacet === null) {
      setFocusRequest(null);
      return;
    }
    setFacet(nextFacet);
    focusSeqRef.current += 1;
    setFocusRequest(focusSeqRef.current);
  };

  // Attention-strip and Equipment-roster navigation (Wall mode only): show the
  // frame's Surface, select it, and open the facet for its health.
  const navigateToFrame = (frameId) => {
    const frame = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
    if (frame === undefined) {
      return;
    }
    setSurfaceId(frame.surface_id);
    selectFrame(frameId, facetFor(frameHealth(snapshot, frameId), facet));
  };

  // Surfaces present in the snapshot, sorted for a deterministic default.
  const surfaces = useMemo(() => {
    const frames = snapshot?.inventory?.frames ?? [];
    return [...new Set(frames.map((frame) => frame.surface_id))].sort();
  }, [snapshot]);

  // Wall mode with frames splits into the plan column and the Inspector column
  // (side by side on wide screens, stacked on narrow ones — CSS only). On the
  // first run there is no frame to inspect, so there is no Inspector column.
  const split =
    snapshot !== null && mode === "wall" && (snapshot.inventory?.frames ?? []).length > 0;

  // Default to the first Surface present; fall back if the chosen one vanished.
  const activeSurface =
    surfaceId !== null && surfaces.includes(surfaceId) ? surfaceId : surfaces[0] ?? null;

  const connect = (event) => {
    event.preventDefault();
    // In-memory only (session.js), mirroring the legacy flat page — never persisted.
    setToken(tokenInput);
    // Trigger one Plane A load with the freshly-set token. A 401 surfaces the
    // auth-rejected state (useSnapshot clears the in-memory token and flags it),
    // rendering the token form again with a "not accepted" message below; any
    // other failure leaves the empty state in place (Bead 18 richer surfacing).
    refresh().catch(() => {});
  };

  return (
    <div className="console">
      <header className="console__header">
        <h1 className="console__title">Operator Console</h1>
        <div
          className="console__mode-toggle"
          role="group"
          aria-label="Console mode"
        >
          <button
            type="button"
            aria-pressed={mode === "wall"}
            className={
              mode === "wall"
                ? "console__mode console__mode--active"
                : "console__mode"
            }
            onClick={() => setMode("wall")}
          >
            Wall
          </button>
          <button
            type="button"
            aria-pressed={mode === "showrunner"}
            className={
              mode === "showrunner"
                ? "console__mode console__mode--active"
                : "console__mode"
            }
            onClick={() => setMode("showrunner")}
          >
            Showrunner
          </button>
        </div>
      </header>

      <form className="console__token" onSubmit={connect}>
        <label className="console__token-field">
          Operator token
          <input
            type="password"
            name="operator-token"
            autoComplete="off"
            value={tokenInput}
            onChange={(event) => setTokenInput(event.target.value)}
          />
        </label>
        <button type="submit">Connect</button>
      </form>

      {authRejected && (
        <p className="console__auth-error" role="alert">
          Operator token was not accepted. Re-enter the token to connect.
        </p>
      )}

      {snapshot !== null && (
        <div
          className="console__statusbar"
          role="group"
          aria-label="Snapshot status"
        >
          <span className="console__age">
            {age === null ? "never updated" : `updated ${age} s ago`}
            {/* Only after a refresh actually failed — never inferred from age. */}
            {refreshFailed && " — last refresh failed"}
          </span>
          <span aria-hidden="true">·</span>
          <button
            type="button"
            className="console__button"
            onClick={() => refresh().catch(() => {})}
          >
            Refresh
          </button>
          <span
            className={`console__health health--${PILL_SEVERITY[health.status] ?? "unknown"}`}
            role="status"
            aria-label={`Central health: ${centralHealth}`}
          >
            {`Central: ${centralHealth}`}
          </span>
        </div>
      )}

      {snapshot !== null && (
        <AttentionStrip
          snapshot={snapshot}
          central={health}
          onNavigate={mode === "wall" ? navigateToFrame : null}
        />
      )}

      {snapshot !== null && <Guidance snapshot={snapshot} />}

      <main className={split ? "console__body console__body--split" : "console__body"}>
        {snapshot === null ? (
          <p>Console ready.</p>
        ) : mode === "showrunner" ? (
          // The show layer. R4 is enforced by COMPOSITION: Showrunner never
          // imports the Commissioning facet, and the Wall-only surfaces below
          // (Plan/Inspector/EquipmentRoster/tray — the only mounts of
          // Commissioning) are simply not rendered in this mode.
          <Showrunner snapshot={snapshot} />
        ) : (
          <>
            <div className="console__main">
              {recovered.length > 0 && (
                <div className="console__recovery" role="status">
                  <p className="console__recovery-text">
                    Recovered — already bound (serial match, not identity):{" "}
                    {recovered.join(", ")}
                  </p>
                  <button type="button" onClick={() => setRecovered([])}>
                    Dismiss
                  </button>
                </div>
              )}
              <div className="console__surface-filter">
                <label className="console__surface-field">
                  Surface
                  <select
                    aria-label="Surface"
                    value={activeSurface ?? ""}
                    onChange={(event) => {
                      setSurfaceId(event.target.value);
                      setSelection(null);
                    }}
                  >
                    {surfaces.map((surface) => (
                      <option key={surface} value={surface}>
                        {surface}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <Plan
                snapshot={snapshot}
                surfaceId={activeSurface}
                selection={selection}
                onSelect={selectFrame}
                onDeleted={() => setSelection(null)}
                regionRef={planRegionRef}
                trayDragRef={trayDragRef}
                onTrayDrop={() => {
                  trayDragRef.current = null;
                }}
              />
              <UnplacedTray
                snapshot={snapshot}
                onSelect={selectFrame}
                onDragStart={(id) => {
                  trayDragRef.current = id;
                }}
                onDeleted={(id) => {
                  setSelection((current) => (current === id ? null : current));
                  planRegionRef.current?.focus();
                }}
              />
              <EquipmentRoster
                snapshot={snapshot}
                bootFacts={bootFacts}
                onNavigate={navigateToFrame}
              />
            </div>
            {split && (
              <aside className="console__side">
                <Inspector
                  snapshot={snapshot}
                  bootFacts={bootFacts}
                  frameId={selection}
                  facet={facet}
                  onFacet={setFacet}
                  focusRequest={focusRequest}
                  onFocusDone={() => setFocusRequest(null)}
                />
              </aside>
            )}
          </>
        )}
      </main>
    </div>
  );
}
