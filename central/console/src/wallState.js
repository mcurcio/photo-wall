import { useMemo, useRef, useState } from "react";

import { facetFor, frameHealth } from "./health.js";
import { DEFAULT_FACET } from "./routes.js";

// The Wall's state that outlives the Wall page (flow design §6): the shell holds it,
// so it lives here, in a module that imports no component. Shell.jsx must not reach
// the Inspector or the Calibration facet except through wallRoutes.jsx (R4;
// tests/test_console_routes_r4.py).

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {{surfaceId: string|null, setSurfaceId: (surfaceId: string|null) => void,
 *            focusRequest: number|null, clearFocus: () => void,
 *            lastWall: Route,
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
 * chosen Surface or a pending focus request, so they live here, above the page. The
 * selected frame and its facet live in the route (`#/wall/frames/<id>/<facet>`). No facet
 * is remembered: a Frame opens on Status unless a visit names its cause (console DDD §61,
 * G3), and the Guidance banner has no dismissal (§54: nothing to dismiss, it leaves when a
 * Frame exists).
 *
 * `lastWall` is the Wall's daily face as last shown, for the sidebar link; Edit layout
 * (`#/wall/layout`) is never remembered, so the Wall link always opens the daily face.
 *
 * Visiting a frame from outside the plan (the attention strip, the Needs attention
 * page, a Player page) shows its Surface, opens the facet that shows its cause
 * (health.js `facetFor`; an ok frame opens Status) and asks the Inspector
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
  const focusSeqRef = useRef(0);
  const lastWallRef = useRef(/** @type {Route} */ ({ section: "wall" }));
  if (route?.section === "wall" && route.mode === undefined) {
    // Idempotent, so safe during render: the Wall as last shown, for the sidebar link.
    lastWallRef.current = route;
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

  const lastWall = lastWallRef.current;
  return useMemo(() => {
    const frameRoute = (frameId) => ({
      section: "wall",
      id: frameId,
      facet: facetFor(frameHealth(snapshot, frameId), DEFAULT_FACET),
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
  }, [surfaceId, focusRequest, lastWall, snapshot, navigate]);
}
