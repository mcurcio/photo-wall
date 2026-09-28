import React, { useRef, useState } from "react";

import { ConfirmAction, deleteFrameRequest } from "./ConfirmAction.jsx";
import { frameHealth } from "./health.js";
import { project } from "./projection.js";

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
 * DELETE (Bead 11; slice 2 §7): each entry carries a Delete control that opens
 * the one confirmation dialog (ConfirmAction), owned here at the tray's top
 * level; a 409 guard message (bound / live Run) is shown inside it. After a
 * delete, `onDeleted` moves focus to the plan region.
 *
 * Each entry also states the frame's health from the one classifier
 * (health.js), the same label its plan tile would show once placed.
 *
 * @param {{snapshot: object|null, onSelect: (frameId: string) => void,
 *          onDragStart?: (frameId: string) => void,
 *          onDeleted?: (frameId: string) => void}} props
 */
export function UnplacedTray({ snapshot, onSelect, onDragStart, onDeleted }) {
  const [confirm, setConfirm] = useState(/** @type {object|null} */ (null));
  const openerRef = useRef(/** @type {HTMLElement|null} */ (null));
  const [status, setStatus] = useState(/** @type {string|null} */ (null));

  const frames = snapshot?.inventory?.frames ?? [];
  const surfaces = [...new Set(frames.map((frame) => frame.surface_id))];
  // The tray is cross-Surface; union each Surface's projected `unplaced` ids.
  // The viewport is irrelevant to the routing decision, so any value works.
  const unplacedIds = surfaces.flatMap(
    (surfaceId) => project(frames, surfaceId, { width: 1, height: 1 }).unplaced,
  );

  const onDelete = (event, frameId) => {
    openerRef.current = event.currentTarget;
    setStatus(null);
    setConfirm({ ...deleteFrameRequest(snapshot, frameId), frameId });
  };

  const onConfirmClosed = (result) => {
    const frameId = confirm?.frameId;
    setConfirm(null);
    if (result?.state === "done") {
      setStatus(result.message);
      onDeleted?.(frameId);
    } else {
      openerRef.current?.focus();
    }
  };

  return (
    <section className="tray" role="group" aria-label="Unplaced frames">
      <h2 className="tray__title">Unplaced frames</h2>
      {unplacedIds.length === 0 ? (
        <p className="tray__empty">No unplaced frames.</p>
      ) : (
        <ul className="tray__list">
          {unplacedIds.map((id) => {
            const health = frameHealth(snapshot, id);
            return (
              <li key={id}>
                <button
                  type="button"
                  className="tray__item"
                  onPointerDown={() => onDragStart?.(id)}
                  onClick={() => onSelect(id)}
                >
                  {id}
                </button>
                <span className={`tray__health health--${health.severity}`}>
                  {health.label}
                </span>
                <button
                  type="button"
                  className="tray__delete"
                  onClick={(event) => onDelete(event, id)}
                >
                  {`Delete frame ${id}`}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <p className="tray__status-line" role="status">
        {status}
      </p>
      {confirm !== null && (
        <ConfirmAction key={confirm.key} request={confirm} onClose={onConfirmClosed} />
      )}
    </section>
  );
}
