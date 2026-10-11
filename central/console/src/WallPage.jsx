import React, { useMemo } from "react";

import { Guidance } from "./Guidance.jsx";
import { LayoutEditor } from "./LayoutEditor.jsx";
import { FramePage } from "./pages/frame-page.tsx";
import { Plan } from "./Plan.jsx";
import { DEFAULT_TAB } from "./routes.js";
import { UnplacedTray } from "./UnplacedTray.jsx";
import { WallUnfinished } from "./WallUnfinished.jsx";

/**
 * @typedef {import("./routes.js").Route} Route
 * @typedef {import("./wallState.js").WallMemory} WallMemory
 */

/**
 * The Wall page (#/wall, #/wall/frames/<id>/<tab>, #/wall/layout; console DDD §61).
 *
 * THE DAILY FACE (#/wall) holds no write (G3): the first-run Guidance (with no Frames), the
 * To finish list (WallUnfinished.jsx), the Surface filter with **Edit layout**, the read-only
 * Plan and the Unplaced tray. Plan and tray are rendered without `edit`, so a drag moves
 * nothing; one click (or Enter) on a Frame opens its page.
 *
 * A FRAME'S PAGE (#/wall/frames/<id>/<tab>) is pages/frame-page.tsx: every setting of one
 * Frame, one tab each (Overview, Position, Picture, Hardware). Opening it pushes a history
 * entry, so Back returns to the plan; switching tab REPLACES the entry.
 *
 * EDIT LAYOUT (#/wall/layout) is LayoutEditor.jsx, which owns every Plan and tray write;
 * its Done returns to the daily face.
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
  const activeSurface =
    memory.surfaceId !== null && surfaces.includes(memory.surfaceId) ? memory.surfaceId : surfaces[0] ?? null;

  if (route.id !== undefined) {
    const frameId = route.id;
    return (
      <FramePage
        key={frameId}
        snapshot={snapshot}
        bootFacts={bootFacts}
        hosts={hosts}
        frameId={frameId}
        tab={route.tab ?? DEFAULT_TAB}
        onTab={(tab) => navigate({ section: "wall", id: frameId, tab }, { replace: true })}
        focusRequest={memory.focusRequest}
        onFocusDone={memory.clearFocus}
      />
    );
  }

  const filter = (
    <div className="console__surface-filter">
      <label className="console__surface-field">
        Surface
        <select
          aria-label="Surface"
          value={activeSurface ?? ""}
          onChange={(event) => memory.setSurfaceId(event.target.value)}
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
        onDone={() => navigate({ section: "wall" })}
      />
    );
  }

  return (
    <div className="console__body">
      <div className="console__main">
        <Guidance
          snapshot={snapshot}
          onAddFirstFrame={() => navigate({ section: "wall", mode: "layout" })}
        />
        <WallUnfinished snapshot={snapshot} />
        {filter}
        <Plan snapshot={snapshot} surfaceId={activeSurface} selection={null} onSelect={memory.openFrame} />
        <UnplacedTray snapshot={snapshot} onSelect={memory.openFrame} />
      </div>
    </div>
  );
}
