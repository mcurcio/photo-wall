import { useMemo, useRef, useState } from "react";

import { frameHealth, tabFor } from "./health.js";
import { DEFAULT_TAB } from "./routes.js";

// The Wall's state that outlives the Wall page (flow design §6): the shell holds it,
// so it lives here, in a module that imports no component. Shell.jsx must not reach
// the Frame page or its live adjustment except through wallRoutes.jsx (R4;
// tests/test_console_routes_r4.py).

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {{surfaceId: string|null, setSurfaceId: (surfaceId: string|null) => void,
 *            focusRequest: number|null, clearFocus: () => void,
 *            lastWall: Route,
 *            frameRoute: (frameId: string) => Route,
 *            prepareVisit: (frameId: string) => void,
 *            visitFrame: (frameId: string) => void,
 *            openFrame: (frameId: string) => void}} WallMemory
 */

/** The snapshot's frame `frameId`, or undefined. */
function findFrame(snapshot, frameId) {
  return (snapshot?.inventory?.frames ?? []).find((frame) => frame.id === frameId);
}

/**
 * What the Wall remembers while it is not mounted (the shell calls this; flow design
 * §6). The Wall page mounts only while it is current, but leaving it must not lose the
 * chosen Surface or a pending focus request, so they live here, above the page. The
 * Frame shown and its tab live in the route (`#/wall/frames/<id>/<tab>`).
 *
 * `lastWall` is the Wall as last shown (the plan, or a Frame's page at a tab), for the
 * sidebar link from another section; Edit layout (`#/wall/layout`) is never remembered.
 *
 * Opening a Frame (a plan tile or tray entry: `openFrame`, at Overview) or visiting one from
 * outside the plan (the attention strip, the Needs attention page, a Player page: the tab that
 * shows its cause, health.js `tabFor`) asks the Frame page to take focus ONCE: `focusRequest`
 * is a fresh number each time, and the page clears it when spent, so a later remount does not
 * refocus.
 *
 * THE SURFACE follows the route: whenever the route comes to name a frame, the chosen Surface
 * becomes that frame's, so the plan shows it when the operator goes back.
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

  // The frame the route last named, once the snapshot lists it.
  const [routedFrameId, setRoutedFrameId] = useState(/** @type {string|null} */ (null));
  const routeFrameId = route?.section === "wall" ? route.id ?? null : null;
  if (routeFrameId !== routedFrameId) {
    // Storing what the last render saw (React's pattern for adjusting state to a prop):
    // a frame the snapshot does not list yet waits for it.
    const frame = routeFrameId === null ? undefined : findFrame(snapshot, routeFrameId);
    if (routeFrameId === null || frame !== undefined) {
      setRoutedFrameId(routeFrameId);
      if (frame !== undefined) {
        setSurfaceId(frame.surface_id);
      }
    }
  }

  const lastWall = lastWallRef.current;
  return useMemo(() => {
    const frameRoute = (frameId) => ({
      section: "wall",
      id: frameId,
      tab: tabFor(frameHealth(snapshot, frameId), DEFAULT_TAB),
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
      openFrame: (frameId) => {
        prepareVisit(frameId);
        navigate({ section: "wall", id: frameId, tab: DEFAULT_TAB });
      },
    };
  }, [surfaceId, focusRequest, lastWall, snapshot, navigate]);
}
