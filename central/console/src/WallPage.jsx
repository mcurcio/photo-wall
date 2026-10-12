import React, { useMemo, useState } from "react";

import { nameWall } from "./framesApi.js";

import { Guidance } from "./Guidance.jsx";
import { LayoutEditor } from "./LayoutEditor.jsx";
import { FramePage } from "./pages/frame-page.tsx";
import { Plan } from "./Plan.jsx";
import { DEFAULT_TAB } from "./routes.js";
import { UnplacedTray } from "./UnplacedTray.jsx";
import { useMutate } from "./useMutate.js";
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
        <WallName key={activeSurface ?? ""} wall={(snapshot?.inventory?.walls ?? []).find((wall) => wall.surface_id === activeSurface) ?? null} />
        <Plan snapshot={snapshot} surfaceId={activeSurface} selection={null} onSelect={memory.openFrame} />
        <UnplacedTray snapshot={snapshot} onSelect={memory.openFrame} />
      </div>
    </div>
  );
}

/** Central's refusal of a wall's name (central/registry.py `name_wall`), in words. */
const WALL_REFUSALS = { unknown_wall: "This wall no longer exists.", unreachable: "Central did not answer. Save again." };

/**
 * The wall's name and its room label (#92), named in place: Rename opens both, Save sends them
 * (PUT /v1/operator/walls/{surface_id}); the snapshot carries them back.
 *
 * @param {{wall: {surface_id: string, name: string, room: string}|null}} props
 */
function WallName({ wall }) {
  const mutate = useMutate();
  const [draft, setDraft] = useState(null);
  const [problem, setProblem] = useState(null);
  const [busy, setBusy] = useState(false);
  if (wall === null) return null;
  if (draft === null) {
    return (
      <div className="console__surface-filter" role="group" aria-label="Wall name">
        <p>{`Wall: ${wall.name}${wall.room === "" ? "" : ` · Room: ${wall.room}`}`}</p>
        <button type="button" className="console__button" onClick={() => { setDraft({ name: wall.name, room: wall.room }); setProblem(null); }}>
          Rename wall
        </button>
      </div>
    );
  }
  const save = async () => {
    const name = draft.name.trim();
    if (name === "" || name.length > 60) {
      setProblem("A wall's name has 1 to 60 characters.");
      return;
    }
    setBusy(true);
    const answer = await mutate(() => nameWall(wall.surface_id, { name, room: draft.room.trim() }));
    setBusy(false);
    if (answer.ok) setDraft(null);
    else setProblem(WALL_REFUSALS[answer.error] ?? `Central refused it (${answer.error}).`);
  };
  return (
    <div className="console__surface-filter" role="group" aria-label="Wall name">
      <label className="console__surface-field">
        Wall name
        <input type="text" maxLength={60} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
      </label>
      <label className="console__surface-field">
        Room
        <input type="text" maxLength={60} value={draft.room} onChange={(event) => setDraft({ ...draft, room: event.target.value })} />
      </label>
      <button type="button" className="console__button" disabled={busy} onClick={() => void save()}>Save wall name</button>
      <button type="button" className="console__button" disabled={busy} onClick={() => setDraft(null)}>Cancel</button>
      {problem !== null && <p role="status">{problem}</p>}
    </div>
  );
}
