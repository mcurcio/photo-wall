import { useEffect, useMemo, useRef, useState } from "react";

import { facetFor, frameHealth } from "./health.js";
import { detectRecovery } from "./recovery.js";

// The Wall's state that outlives the Wall page (flow design §6): the shell holds it,
// so it lives here, in a module that imports no component. Shell.jsx must not reach
// the Inspector or Commissioning except through wallRoutes.jsx (R4;
// tests/test_console_routes_r4.py).

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {import("./routes.js").Facet} Facet
 * @typedef {{surfaceId: string|null, setSurfaceId: (surfaceId: string|null) => void,
 *            focusRequest: number|null, clearFocus: () => void,
 *            guidanceDismissed: boolean, dismissGuidance: () => void,
 *            lastFacet: Facet, lastWall: Route,
 *            frameRoute: (frameId: string) => Route,
 *            prepareVisit: (frameId: string) => void,
 *            visitFrame: (frameId: string) => void,
 *            selectPlainly: (frameId: string, surfaceId: string|null) => void}} WallMemory
 */

/** The snapshot's frame `frameId`, or undefined. */
function findFrame(snapshot, frameId) {
  return (snapshot?.inventory?.frames ?? []).find((frame) => frame.id === frameId);
}

/**
 * What the Wall remembers while it is not mounted (the shell calls this; flow design
 * §6). The Wall page mounts only while it is current, but leaving it must not lose the
 * chosen Surface, the facet last open, the Guidance dismissal or a pending focus
 * request, so they live here, above the page. The selected frame and its facet live in
 * the route (`#/wall/frames/<id>/<facet>`).
 *
 * Visiting a frame from outside the plan (the attention strip, the Needs attention
 * page, a Player page) shows its Surface, opens the facet that shows its cause
 * (health.js `facetFor`; an ok frame keeps the facet last open) and asks the Inspector
 * to take focus ONCE: `focusRequest` is a fresh number each time, and the Inspector
 * clears it when spent, so a later remount does not refocus. Plain selection on the
 * plan or tray (`selectPlainly`) clears it and never moves focus.
 *
 * THE SURFACE follows the route: whenever the route comes to name a frame by any way
 * but plain selection (a typed URL, Back or Forward, a link), the chosen Surface
 * becomes that frame's, so the plan shows what the Inspector does. Plain selection
 * keeps the Surface in view, even for an unplaced frame of another Surface.
 *
 * The returned object is the same between renders until something in it changes, so
 * the shell's route context, and the hidden Show pages, stay still on unrelated renders.
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

  // The frame the route last named, once the snapshot lists it, and the frame plain
  // selection is about to route to (that one keeps the Surface).
  const [routedFrameId, setRoutedFrameId] = useState(/** @type {string|null} */ (null));
  const [plainFrameId, setPlainFrameId] = useState(/** @type {string|null} */ (null));
  const routeFrameId = route?.section === "wall" ? route.id ?? null : null;
  if (routeFrameId !== routedFrameId) {
    // Storing what the last render saw (React's pattern for adjusting state to a prop):
    // a frame the snapshot does not list yet waits for it.
    const frame = routeFrameId === null ? undefined : findFrame(snapshot, routeFrameId);
    if (routeFrameId === null || frame !== undefined) {
      setRoutedFrameId(routeFrameId);
      if (frame !== undefined && plainFrameId !== routeFrameId) {
        setSurfaceId(frame.surface_id);
      }
      setPlainFrameId(null);
    }
  }

  const lastFacet = lastFacetRef.current;
  const lastWall = lastWallRef.current;
  return useMemo(() => {
    const frameRoute = (frameId) => ({
      section: "wall",
      id: frameId,
      facet: facetFor(frameHealth(snapshot, frameId), lastFacetRef.current),
    });
    const prepareVisit = (frameId) => {
      const frame = findFrame(snapshot, frameId);
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
      lastFacet,
      lastWall,
      frameRoute,
      prepareVisit,
      visitFrame: (frameId) => {
        if (findFrame(snapshot, frameId) === undefined) {
          return;
        }
        prepareVisit(frameId);
        navigate(frameRoute(frameId));
      },
      selectPlainly: (frameId, keepSurfaceId) => {
        setPlainFrameId(frameId);
        setFocusRequest(null);
        setSurfaceId(keepSurfaceId);
      },
    };
  }, [surfaceId, focusRequest, guidanceDismissed, lastFacet, lastWall, snapshot, navigate]);
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
  return useMemo(() => ({ recovered, dismiss: () => setRecovered([]) }), [recovered]);
}
