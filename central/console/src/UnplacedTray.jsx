import React from "react";

import { project } from "./projection.js";

/**
 * Read-only Unplaced tray (Bead 1 / tracer).
 *
 * Lists the legacy origin-stacked / geometry-less frames across every Surface,
 * keyed and labelled by frame id (identity), each selectable via `onSelect`.
 * These are frames the old flat UI created at `wall`/(0,0); rather than piling
 * them at the origin on the plan they surface here as an entry state (design
 * §1/§6, J3), from which later beads let the operator drag them onto the plan.
 *
 * Membership is derived from the SAME `project()` routing the plan uses — the
 * tray shows exactly the frames the projection sends to `unplaced`, so the plan
 * and tray can never disagree about a frame's fate.
 *
 * @param {{snapshot: object|null, onSelect: (frameId: string) => void}} props
 */
export function UnplacedTray({ snapshot, onSelect }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const surfaces = [...new Set(frames.map((frame) => frame.surface_id))];
  // The tray is cross-Surface; union each Surface's projected `unplaced` ids.
  // The viewport is irrelevant to the routing decision, so any value works.
  const unplacedIds = surfaces.flatMap(
    (surfaceId) => project(frames, surfaceId, { width: 1, height: 1 }).unplaced,
  );

  return (
    <section className="tray" role="group" aria-label="Unplaced frames">
      <h2 className="tray__title">Unplaced frames</h2>
      {unplacedIds.length === 0 ? (
        <p className="tray__empty">No unplaced frames.</p>
      ) : (
        <ul className="tray__list">
          {unplacedIds.map((id) => (
            <li key={id}>
              <button
                type="button"
                className="tray__item"
                onClick={() => onSelect(id)}
              >
                {id}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
