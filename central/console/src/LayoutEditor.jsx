import React, { useEffect, useRef, useState } from "react";

import { deleteFrameRequest, useConfirm } from "./ConfirmAction.jsx";
import { createFrame, dropFromTray, moveFrame } from "./framesApi.js";
import { Plan } from "./Plan.jsx";
import { UnplacedTray } from "./UnplacedTray.jsx";
import { useMutate } from "./useMutate.js";

/**
 * Edit layout (`#/wall/layout`; console DDD §61, G3): the Wall's mode for drawing, moving
 * and deleting Frames, and for the Unplaced tray's drag-out and Delete. It OWNS every Plan
 * and tray write (framesApi writes through `useMutate`, the delete confirmation) and hands
 * them to the select-only Plan.jsx and UnplacedTray.jsx as `edit` handlers, so the Wall's
 * daily face, which renders those two without `edit`, holds no write. There is no Inspector
 * here. **Done** calls `onDone(frameId|null)` with the Frame selected here, and the Wall
 * returns to its daily face (that Frame's Status when one is selected).
 *
 * The selection is this mode's own (the route names no Frame); it starts at `initialFrameId`,
 * the Frame the daily face last showed.
 *
 * Unplaced-tray drag-out (Bead 11). The dragged frame id lives in a REF so the plan's
 * pointer-up reads it synchronously (a full press->move->release can fire before React
 * re-renders — cf. Plan's own dragRef). A window-level pointer-up clears it so a press
 * that does NOT land on the plan (a plain tray click, or a release anywhere else)
 * cancels the drag rather than leaving a stale id that a later plan release would
 * wrongly consume. The plan's own handler runs first (React binds at the root, below
 * window in the bubble path), so a genuine drop is read and cleared before this reset.
 *
 * @param {{snapshot: object, surfaceId: string|null, filter: React.ReactNode,
 *          initialFrameId: string|null, onDone: (frameId: string|null) => void}} props
 */
export function LayoutEditor({ snapshot, surfaceId, filter, initialFrameId, onDone }) {
  const mutate = useMutate();
  const [chosen, setChosen] = useState(/** @type {string|null} */ (initialFrameId));
  const trayDragRef = useRef(/** @type {string|null} */ (null));
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
  const selection = frames.some((frame) => frame.id === chosen) ? chosen : null;
  const focusPlan = () => planRegionRef.current?.focus();
  const forget = (frameId) => setChosen((current) => (current === frameId ? null : current));
  const planDelete = useConfirm(focusPlan, (result, request) => {
    forget(request.frameId);
    focusPlan();
  });
  const trayDelete = useConfirm(null, (result, request) => {
    forget(request.frameId);
    focusPlan();
  });
  const deleteRequest = (frameId) => ({ ...deleteFrameRequest(snapshot, frameId), frameId });

  /** @type {import("./Plan.jsx").PlanEdit} */
  const planEdit = {
    createFrame: (id, placement, profile) => mutate(() => createFrame(id, placement, profile)),
    moveFrame: (frameId, placement) => mutate(() => moveFrame(frameId, placement)),
    takeTrayDrag: () => {
      const frameId = trayDragRef.current;
      trayDragRef.current = null;
      return frameId;
    },
    dropFromTray: (frameId, pxRect, viewport, surface) => {
      mutate(() => dropFromTray(frameId, pxRect, viewport, surface)).catch(() => {});
    },
    deleteFrame: (event, frameId) => planDelete.open(event, deleteRequest(frameId)),
    status: planDelete.confirmation("plan__status-line"),
  };
  /** @type {import("./UnplacedTray.jsx").TrayEdit} */
  const trayEdit = {
    dragStart: (frameId) => {
      trayDragRef.current = frameId;
    },
    deleteFrame: (event, frameId) => trayDelete.open(event, deleteRequest(frameId)),
    status: trayDelete.confirmation("tray__status-line"),
  };

  return (
    <div className="console__body">
      <div className="console__main">
        <div className="layout__bar" role="group" aria-label="Editing layout">
          <h2 className="layout__title">Editing layout</h2>
          <button type="button" className="console__button" onClick={() => onDone(selection)}>
            Done
          </button>
        </div>
        {filter}
        <Plan
          snapshot={snapshot}
          surfaceId={surfaceId}
          selection={selection}
          onSelect={setChosen}
          regionRef={planRegionRef}
          edit={planEdit}
        />
        <UnplacedTray snapshot={snapshot} onSelect={setChosen} edit={trayEdit} />
      </div>
    </div>
  );
}
