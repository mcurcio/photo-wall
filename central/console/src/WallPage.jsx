import React, { useMemo } from "react";

import { Guidance } from "./Guidance.jsx";
import { Inspector } from "./Inspector.jsx";
import { LayoutEditor } from "./LayoutEditor.jsx";
import { Plan } from "./Plan.jsx";
import { DEFAULT_FACET, routeIdName } from "./routes.js";
import { UnplacedTray } from "./UnplacedTray.jsx";
import { WallUnfinished } from "./WallUnfinished.jsx";

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {import("./wallState.js").WallMemory} WallMemory
 */

/**
 * The Wall page (#/wall, #/wall/frames/<id>/<facet>, #/wall/layout; console DDD §61).
 *
 * THE DAILY FACE (#/wall, #/wall/frames/…) holds no write (G3): the first-run Guidance
 * (with no Frames), the To finish list (WallUnfinished.jsx), the Surface filter with
 * **Edit layout**, the read-only Plan, the select-only Unplaced tray and the Frame
 * Inspector. Plan and tray are rendered without `edit`, so a drag moves nothing.
 *
 * EDIT LAYOUT (#/wall/layout) is LayoutEditor.jsx, which owns every Plan and tray write;
 * its Done returns here (to the Frame it had selected, at Status).
 *
 * The selected frame and its open facet are the route's; a Frame opens on Status
 * (`DEFAULT_FACET`) unless the route names a facet. Selecting a frame on the plan or tray
 * opens its Status, and switching facet, REPLACES the history entry (so Back leaves the
 * Wall rather than stepping through selections) and never moves focus. A frame id that the
 * loaded snapshot does not list reads "This no longer exists".
 *
 * @param {{snapshot: object, bootFacts: object|null, route: Route,
 *          hosts?: import("./fleetHosts.js").FleetHosts|null,
 *          navigate: (route: Route, options?: {replace?: boolean}) => void,
 *          memory: WallMemory}} props
 */
export function WallPage({ snapshot, bootFacts, hosts = null, route, navigate, memory }) {
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
  const facet = route.facet ?? DEFAULT_FACET;

  // The chosen Surface; before one is chosen, a deep-linked frame's, then the first.
  const activeSurface =
    memory.surfaceId !== null && surfaces.includes(memory.surfaceId)
      ? memory.surfaceId
      : routeFrame?.surface_id ?? surfaces[0] ?? null;

  const wallRoute = () => navigate({ section: "wall" }, { replace: true });

  // Plain selection (plan or tray): keeps the Surface in view, opens Status, and never
  // moves focus.
  const selectFrame = (frameId) => {
    memory.selectPlainly(frameId, activeSurface);
    navigate({ section: "wall", id: frameId, facet: DEFAULT_FACET }, { replace: true });
  };

  const filter = (
    <div className="console__surface-filter">
      <label className="console__surface-field">
        Surface
        <select
          aria-label="Surface"
          value={activeSurface ?? ""}
          onChange={(event) => {
            memory.setSurfaceId(event.target.value);
            if (route.mode === undefined) {
              wallRoute();
            }
          }}
        >
          {surfaces.map((surface) => (
            <option key={surface} value={surface}>
              {surface}
            </option>
          ))}
        </select>
      </label>
      {route.mode === undefined && (
        <button
          type="button"
          className="console__button"
          onClick={() => navigate({ section: "wall", mode: "layout" })}
        >
          Edit layout
        </button>
      )}
    </div>
  );

  if (route.mode === "layout") {
    return (
      <LayoutEditor
        snapshot={snapshot}
        surfaceId={activeSurface}
        filter={filter}
        initialFrameId={memory.lastWall.id ?? null}
        onDone={(frameId) =>
          navigate(frameId === null ? { section: "wall" }
            : { section: "wall", id: frameId, facet: DEFAULT_FACET })}
      />
    );
  }

  // On the first run there is no frame to inspect, so there is no Inspector column;
  // with frames, the plan column and the Inspector sit side by side on wide screens
  // and stack on narrow ones (CSS only).
  const split = frames.length > 0;

  return (
    <div className={split ? "console__body console__body--split" : "console__body"}>
      <div className="console__main">
        <Guidance
          snapshot={snapshot}
          onAddFirstFrame={() => navigate({ section: "wall", mode: "layout" })}
        />
        <WallUnfinished snapshot={snapshot} />
        {filter}
        <Plan
          snapshot={snapshot}
          surfaceId={activeSurface}
          selection={selection}
          onSelect={selectFrame}
        />
        <UnplacedTray snapshot={snapshot} onSelect={selectFrame} />
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
              hosts={hosts}
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
