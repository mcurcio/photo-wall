import React, { useEffect, useMemo, useRef } from "react";

import { Guidance } from "./Guidance.jsx";
import { Inspector } from "./Inspector.jsx";
import { Plan } from "./Plan.jsx";
import { routeIdName } from "./routes.js";
import { UnplacedTray } from "./UnplacedTray.jsx";

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {import("./wallState.js").WallMemory} WallMemory
 */

/**
 * The Wall page (#/wall, #/wall/frames/<id>/<facet>): the first-run Guidance, the Surface filter, the per-Surface Plan, the Unplaced tray and
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
 *          memory: WallMemory}} props
 */
export function WallPage({ snapshot, bootFacts, route, navigate, memory }) {
  const trayDragRef = useRef(/** @type {string|null} */ (null));
  // Reuse the Plan's measured-create action so onboarding opens the same form as
  // the existing control, with the Plan remaining the owner of creation state.
  const addFrameButtonRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
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
  // Surfaces present in the snapshot, sorted for a deterministic default. A new
  // installation has no Frames to derive one from, but the first drag still
  // needs the registry's default Surface to create its first Frame.
  const surfaces = useMemo(
    () => {
      const existing = [...new Set(frames.map((frame) => frame.surface_id))].sort();
      return existing.length > 0 ? existing : ["wall"];
    },
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
    memory.selectPlainly(frameId, activeSurface);
    navigate({ section: "wall", id: frameId, facet }, { replace: true });
  };

  // On the first run there is no frame to inspect, so there is no Inspector column;
  // with frames, the plan column and the Inspector sit side by side on wide screens
  // and stack on narrow ones (CSS only).
  const split = frames.length > 0;

  return (
    <div className={split ? "console__body console__body--split" : "console__body"}>
      <div className="console__main">
        <Guidance
          snapshot={snapshot}
          dismissed={memory.guidanceDismissed}
          onAddFirstFrame={() => addFrameButtonRef.current?.click()}
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
          addFrameButtonRef={addFrameButtonRef}
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
              <p className="inspector__empty">
                {`${routeIdName("Frame", routeFrameId, { start: true })}: This no longer exists.`}
              </p>
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
