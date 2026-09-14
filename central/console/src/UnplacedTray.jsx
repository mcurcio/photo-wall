import React, { useState } from "react";

import { deleteFrame } from "./Plan.jsx";
import { project } from "./projection.js";
import { useMutate } from "./useMutate.js";

/**
 * Unplaced tray (Bead 1 read-only tracer; Bead 11 drag-out + delete).
 *
 * Lists the legacy origin-stacked / geometry-less frames across every Surface,
 * keyed and labelled by frame id (identity), each selectable via `onSelect`.
 * These are frames the old flat UI created at `wall`/(0,0); rather than piling
 * them at the origin on the plan they surface here as an entry state (design
 * §1/§6, J3), from which the operator drags them onto the plan (PATCH, giving
 * them distinct geometry so they leave the tray) or removes them (DELETE).
 *
 * Membership is derived from the SAME `project()` routing the plan uses — the
 * tray shows exactly the frames the projection sends to `unplaced`, so the plan
 * and tray can never disagree about a frame's fate.
 *
 * DRAG-OUT (Bead 11, design J3/§12): a pointer press on a tray entry starts a
 * drag whose frame id is recorded by the caller (`onDragStart`); releasing over
 * the plan is handled by {@link Plan}'s pointer-up, which PATCHes a distinct
 * position. A press-release ON the entry (no drag onto the plan) stays a plain
 * click and selects the frame.
 *
 * DELETE (Bead 11): each entry carries a Delete control that removes the frame
 * (DELETE); a 409 guard message (bound / live Run) is surfaced verbatim.
 *
 * @param {{snapshot: object|null, onSelect: (frameId: string) => void,
 *          onDragStart?: (frameId: string) => void}} props
 */
export function UnplacedTray({ snapshot, onSelect, onDragStart }) {
  const mutate = useMutate();
  const [deleteError, setDeleteError] = useState(/** @type {string|null} */ (null));

  const frames = snapshot?.inventory?.frames ?? [];
  const surfaces = [...new Set(frames.map((frame) => frame.surface_id))];
  // The tray is cross-Surface; union each Surface's projected `unplaced` ids.
  // The viewport is irrelevant to the routing decision, so any value works.
  const unplacedIds = surfaces.flatMap(
    (surfaceId) => project(frames, surfaceId, { width: 1, height: 1 }).unplaced,
  );

  const onDelete = (frameId) => {
    setDeleteError(null);
    mutate(() => deleteFrame(frameId))
      .then((result) => {
        if (!result.ok) {
          // Surface the design §9a guard wording verbatim.
          setDeleteError(result.message);
        }
      })
      .catch(() => setDeleteError("Could not delete the frame."));
  };

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
                onPointerDown={() => onDragStart?.(id)}
                onClick={() => onSelect(id)}
              >
                {id}
              </button>
              <button
                type="button"
                className="tray__delete"
                onClick={() => onDelete(id)}
              >
                {`Delete frame ${id}`}
              </button>
            </li>
          ))}
        </ul>
      )}
      {deleteError != null && (
        <p className="tray__delete-error" role="alert">
          {deleteError}
        </p>
      )}
    </section>
  );
}
