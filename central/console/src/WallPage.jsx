import React, { useEffect, useMemo, useRef, useState } from "react";

import { Guidance } from "./Guidance.jsx";
import { facetFor, frameHealth } from "./health.js";
import { Inspector } from "./Inspector.jsx";
import { Plan } from "./Plan.jsx";
import { detectRecovery } from "./recovery.js";
import { UnplacedTray } from "./UnplacedTray.jsx";

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {import("./routes.js").Facet} Facet
 * @typedef {{surfaceId: string|null, setSurfaceId: (surfaceId: string|null) => void,
 *            focusRequest: number|null, clearFocus: () => void,
 *            guidanceDismissed: boolean, dismissGuidance: () => void,
 *            lastFacet: Facet, lastWall: Route,
 *            frameRoute: (frameId: string) => Route,
 *            prepareVisit: (frameId: string) => void,
 *            visitFrame: (frameId: string) => void}} WallMemory
 */

/**
 * What the Wall remembers while it is not mounted (the shell calls this; flow design
 * §6). The Wall page mounts only while it is current, but leaving it must not lose the
 * chosen Surface, the facet last open, the Guidance dismissal or a pending focus
 * request, so they live here, above the page. The selected frame and its facet live in
 * the route (`#/wall/frames/<id>/<facet>`).
 *
 * Visiting a frame from outside the plan (the attention strip, the Needs attention
 * page, the Equipment roster) shows its Surface, opens the facet that shows its cause
 * (health.js `facetFor`; an ok frame keeps the facet last open) and asks the Inspector
 * to take focus ONCE: `focusRequest` is a fresh number each time, and the Inspector
 * clears it when spent, so a later remount does not refocus. Plain selection on the
 * plan or tray clears it and never moves focus.
 *
 * @param {Route|null} route
 * @param {object|null} snapshot
 * @param {(route: Route, options?: {replace?: boolean}) => void} navigate
 * @returns {WallMemory}
 */
export function useWallMemory(route, snapshot, navigate) {
  const [surfaceId, setSurfaceId] = useState(/** @type {string|null} */ (null));
  const [focusRequest, setFocusRequest] = useState(/** @type {number|null} */ (null));
  const [guidanceDismissed, setGuidanceDismissed] = useState(false);
  const focusSeqRef = useRef(0);
  const lastFacetRef = useRef(/** @type {Facet} */ ("commissioning"));
  const lastWallRef = useRef(/** @type {Route} */ ({ section: "wall" }));
  if (route?.section === "wall") {
    // Idempotent, so safe during render: the Wall as last shown, for the sidebar link.
    lastWallRef.current = route;
    if (route.facet !== undefined) {
      lastFacetRef.current = route.facet;
    }
  }

  const findFrame = (frameId) =>
    (snapshot?.inventory?.frames ?? []).find((frame) => frame.id === frameId);

  const frameRoute = (frameId) => ({
    section: "wall",
    id: frameId,
    facet: facetFor(frameHealth(snapshot, frameId), lastFacetRef.current),
  });

  const prepareVisit = (frameId) => {
    const frame = findFrame(frameId);
    if (frame !== undefined) {
      setSurfaceId(frame.surface_id);
    }
    focusSeqRef.current += 1;
    setFocusRequest(focusSeqRef.current);
  };

  return {
    surfaceId,
    setSurfaceId,
    focusRequest,
    clearFocus: () => setFocusRequest(null),
    guidanceDismissed,
    dismissGuidance: () => setGuidanceDismissed(true),
    lastFacet: lastFacetRef.current,
    lastWall: lastWallRef.current,
    frameRoute,
    prepareVisit,
    visitFrame: (frameId) => {
      if (findFrame(frameId) === undefined) {
        return;
      }
      prepareVisit(frameId);
      navigate(frameRoute(frameId));
    },
  };
}

/**
 * Auto-recovery banner state (design J1, §1a D-a). Recovery is INFERRED by diffing
 * the CURRENT Plane A snapshot against the PRIOR one, so the prior snapshot is kept
 * here. The shell calls this, so every snapshot is seen even while the Wall is not
 * mounted; the banner shows on the Wall. It surfaces "a known Pi returned already
 * bound" and is suppressed on the true first run (no prior snapshot) by detectRecovery.
 *
 * @param {object|null} snapshot
 * @returns {{recovered: string[], dismiss: () => void}}
 */
export function useRecovery(snapshot) {
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
  return { recovered, dismiss: () => setRecovered([]) };
}

/**
 * The Wall page (#/wall, #/wall/frames/<id>/<facet>): the recovery banner, the
 * first-run Guidance, the Surface filter, the per-Surface Plan, the Unplaced tray and
 * the Frame Inspector.
 *
 * The selected frame and its open facet are the route's. Selecting a frame on the plan
 * or tray, or switching facet, REPLACES the history entry (so Back leaves the Wall
 * rather than stepping through selections) and never moves focus. A frame id that the
 * loaded snapshot does not list reads "This no longer exists".
 *
 * Unplaced-tray drag-out (Bead 11). The dragged frame id lives in a REF so the plan's
 * pointer-up reads it synchronously (a full press->move->release can fire before React
 * re-renders — cf. Plan's own dragRef). A window-level pointer-up clears it so a press
 * that does NOT land on the plan (a plain tray click, or a release anywhere else)
 * cancels the drag rather than leaving a stale id that a later plan release would
 * wrongly consume. The plan's own handler runs first (React binds at the root, below
 * window in the bubble path), so a genuine drop is read and cleared before this reset.
 *
 * @param {{snapshot: object, bootFacts: object|null, route: Route,
 *          navigate: (route: Route, options?: {replace?: boolean}) => void,
 *          memory: WallMemory, recovery: {recovered: string[], dismiss: () => void}}} props
 */
export function WallPage({ snapshot, bootFacts, route, navigate, memory, recovery }) {
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

  const frames = snapshot?.inventory?.frames ?? [];
  // Surfaces present in the snapshot, sorted for a deterministic default.
  const surfaces = useMemo(
    () => [...new Set(frames.map((frame) => frame.surface_id))].sort(),
    [frames],
  );
  const routeFrameId = route.id ?? null;
  const routeFrame =
    routeFrameId === null ? undefined : frames.find((frame) => frame.id === routeFrameId);
  const stale = routeFrameId !== null && routeFrame === undefined;
  const selection = stale ? null : routeFrameId;
  const facet = route.facet ?? memory.lastFacet;

  // The chosen Surface; before one is chosen, a deep-linked frame's, then the first.
  const activeSurface =
    memory.surfaceId !== null && surfaces.includes(memory.surfaceId)
      ? memory.surfaceId
      : routeFrame?.surface_id ?? surfaces[0] ?? null;

  const wallRoute = () => navigate({ section: "wall" }, { replace: true });

  // Plain selection (plan or tray): keeps the Surface in view and the open facet, and
  // never moves focus.
  const selectFrame = (frameId) => {
    memory.clearFocus();
    memory.setSurfaceId(activeSurface);
    navigate({ section: "wall", id: frameId, facet }, { replace: true });
  };

  // On the first run there is no frame to inspect, so there is no Inspector column;
  // with frames, the plan column and the Inspector sit side by side on wide screens
  // and stack on narrow ones (CSS only).
  const split = frames.length > 0;

  return (
    <div className={split ? "console__body console__body--split" : "console__body"}>
      <div className="console__main">
        {recovery.recovered.length > 0 && (
          <div className="console__recovery" role="status">
            <p className="console__recovery-text">
              Recovered — already bound (serial match, not identity):{" "}
              {recovery.recovered.join(", ")}
            </p>
            <button type="button" onClick={recovery.dismiss}>
              Dismiss
            </button>
          </div>
        )}
        <Guidance
          snapshot={snapshot}
          dismissed={memory.guidanceDismissed}
          onDismiss={memory.dismissGuidance}
        />
        <div className="console__surface-filter">
          <label className="console__surface-field">
            Surface
            <select
              aria-label="Surface"
              value={activeSurface ?? ""}
              onChange={(event) => {
                memory.setSurfaceId(event.target.value);
                wallRoute();
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
          onDeleted={wallRoute}
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
            if (id === routeFrameId) {
              wallRoute();
            }
            planRegionRef.current?.focus();
          }}
        />
      </div>
      {split && (
        <aside className="console__side">
          {stale ? (
            <section className="inspector inspector--empty" role="region" aria-label="Inspector">
              <p className="inspector__empty">{`Frame ${routeFrameId}: This no longer exists.`}</p>
            </section>
          ) : (
            <Inspector
              snapshot={snapshot}
              bootFacts={bootFacts}
              frameId={selection}
              facet={facet}
              onFacet={(next) =>
                navigate({ section: "wall", id: selection, facet: next }, { replace: true })
              }
              focusRequest={memory.focusRequest}
              onFocusDone={memory.clearFocus}
            />
          )}
        </aside>
      )}
    </div>
  );
}
