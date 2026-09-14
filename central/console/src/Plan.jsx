import React, { useEffect, useRef, useState } from "react";

import { createFrame, deleteFrame, dropFromTray, moveFrame } from "./framesApi.js";
import { connectivity, nowShowing } from "./join.js";
import { dragToPlacement, orientationCoherent, project } from "./projection.js";
import { useMutate } from "./useMutate.js";

/**
 * Per-Surface plan (Bead 1 read-only tracer; Bead 2 status chips; Bead 10 spatial
 * editing — drag-to-create + drag-to-move).
 *
 * Renders the selected Surface's placed frames as hand-coded SVG `<rect>`
 * elements from their committed `x_mm/y_mm/width_mm/height_mm`, scaled to fit the
 * viewport by the pure `project()` helper. Each frame is selectable — clicking
 * (or Enter/Space on the focused frame) calls `onSelect(frameId)`; `selection` is
 * the currently-selected `frameId|null`. Every frame carries an accessible name
 * that embeds its frame id, so tests locate frames by identity via role/text —
 * never by coordinates (design §1c, tracer testing philosophy).
 *
 * Alongside each drawn frame the plan renders a status readout (Bead 2): the
 * intended now-showing chip "Scheduled: <scene_id>" + phase (from `nowShowing`),
 * a connectivity dot (from `connectivity`), and a `calibration_valid` badge. The
 * chip asserts operator INTENT, never confirmed playback — the word "LIVE" is
 * deliberately absent (design §6a).
 *
 * SPATIAL EDITING (Bead 10, design J3/§9a): a pointer drag on EMPTY canvas draws
 * an in-progress rectangle (Plane B, held as component-local drag state) and, on
 * release, opens a minimal new-frame form to capture the display `FrameProfile`;
 * submitting POSTs a new Frame via {@link createFrame}. A pointer drag that starts
 * ON an existing frame repositions it via {@link moveFrame} (`PATCH`,
 * last-write-wins, no token — §9a). Both writes go through the shared
 * `useMutate()` hook so the plan corrects from the next Plane A snapshot. A press
 * that does not move beyond a small threshold stays a plain click (selection).
 *
 * Origin-stacked / geometry-less frames are NOT drawn here; they belong to the
 * Unplaced tray (see UnplacedTray.jsx).
 *
 * @param {{snapshot: object|null, surfaceId: string|null,
 *          selection: string|null, onSelect: (frameId: string) => void}} props
 */
const VIEWPORT = { width: 960, height: 600 };

// Movement (viewBox px) a press must exceed before it counts as a drag rather
// than a click. Below this, a press-release on a frame selects it.
const DRAG_THRESHOLD = 6;

const CONNECTIVITY_LABEL = {
  connected: "Player connected",
  disconnected: "Player disconnected",
  unbound: "Player unbound",
};

/**
 * Normalize the two drag endpoints (viewBox px) into a top-left rect `{x,y,w,h}`.
 */
function normRect(start, cur) {
  return {
    x: Math.min(start.x, cur.x),
    y: Math.min(start.y, cur.y),
    w: Math.abs(cur.x - start.x),
    h: Math.abs(cur.y - start.y),
  };
}

/** Map a pointer event to viewBox px via the SVG's on-screen box (no letterbox:
 * `.plan__svg` is `width:100%; height:auto`, preserving the viewBox aspect). */
function toViewbox(svg, event) {
  const box = svg.getBoundingClientRect();
  return {
    x: ((event.clientX - box.left) / box.width) * VIEWPORT.width,
    y: ((event.clientY - box.top) / box.height) * VIEWPORT.height,
  };
}

const PROFILE_DEFAULTS = { width_px: 1920, height_px: 1080, diagonal_inches: 24, video: true };

export function Plan({
  snapshot,
  surfaceId,
  selection,
  onSelect,
  onDeleted,
  trayDragRef,
  onTrayDrop,
}) {
  const mutate = useMutate();
  const svgRef = useRef(null);
  // Plane B: the LIVE in-progress drag (create or move). Held in a ref, not state,
  // because a full pointerdown->move->up sequence can fire before React re-renders
  // — the move/up handlers must read the drag synchronously (cf. tryingRef in
  // useCalibration). `draft` mirrors it purely to render the in-progress rectangle.
  const dragRef = useRef(/** @type {object|null} */ (null));
  const [draft, setDraft] = useState(/** @type {object|null} */ (null));
  // The pending new-frame drag rect awaiting a profile from the form (px).
  const [newFrame, setNewFrame] = useState(/** @type {{pxRect: object}|null} */ (null));
  const [profile, setProfile] = useState(PROFILE_DEFAULTS);
  const [formError, setFormError] = useState(/** @type {string|null} */ (null));
  // The guard message from a refused DELETE (design §9a), cleared on the next attempt.
  const [deleteError, setDeleteError] = useState(/** @type {string|null} */ (null));
  // True once the current press has moved past the threshold — read by a frame's
  // onClick so a drag-move is not also treated as a selection.
  const didDragRef = useRef(false);

  // A guard message is about the frame it was raised for; drop it when the
  // selection moves so one frame's refusal never lingers over another.
  useEffect(() => {
    setDeleteError(null);
  }, [selection]);

  const frames = snapshot?.inventory?.frames ?? [];
  const framesById = new Map(frames.map((frame) => [frame.id, frame]));
  const { placed } = project(frames, surfaceId, VIEWPORT);

  const beginCreate = (event) => {
    if (surfaceId == null || newFrame != null) {
      return;
    }
    const svg = svgRef.current;
    try {
      svg.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is best-effort; drag still tracks via SVG-level events.
    }
    const start = toViewbox(svg, event);
    didDragRef.current = false;
    dragRef.current = { mode: "create", start, cur: start };
    setDraft(normRect(start, start));
  };

  const beginMove = (event, id, rect) => {
    // Do not let the SVG's create-drag also start; this press owns a move.
    event.stopPropagation();
    const svg = svgRef.current;
    try {
      svg.setPointerCapture(event.pointerId);
    } catch {
      // best-effort
    }
    const start = toViewbox(svg, event);
    didDragRef.current = false;
    dragRef.current = { mode: "move", frameId: id, rect, start, cur: start };
    setDraft({ ...rect });
  };

  const onPointerMove = (event) => {
    const drag = dragRef.current;
    if (drag == null) {
      return;
    }
    const cur = toViewbox(svgRef.current, event);
    drag.cur = cur;
    if (
      Math.abs(cur.x - drag.start.x) > DRAG_THRESHOLD ||
      Math.abs(cur.y - drag.start.y) > DRAG_THRESHOLD
    ) {
      didDragRef.current = true;
    }
    setDraft(
      drag.mode === "create"
        ? normRect(drag.start, cur)
        : {
            x: drag.rect.x + (cur.x - drag.start.x),
            y: drag.rect.y + (cur.y - drag.start.y),
            w: drag.rect.w,
            h: drag.rect.h,
          },
    );
  };

  const onPointerUp = (event) => {
    // A drag that STARTED in the Unplaced tray (App holds its frame id in a ref so
    // the value is read synchronously here, free of stale-closure risk) and is
    // RELEASED over the plan drops that frame onto the plan: PATCH a distinct
    // position so it leaves the tray (design J3/§12). The tray press captured no
    // pointer on the SVG, so `dragRef` is null — this branch owns the release.
    const trayFrameId = trayDragRef?.current ?? null;
    if (trayFrameId != null) {
      onTrayDrop?.();
      if (surfaceId != null) {
        const pt = toViewbox(svgRef.current, event);
        mutate(() =>
          dropFromTray(trayFrameId, { x: pt.x, y: pt.y, w: 1, h: 1 }, VIEWPORT, surfaceId),
        ).catch(() => {});
      }
      return;
    }
    const finished = dragRef.current;
    if (finished == null) {
      return;
    }
    const svg = svgRef.current;
    try {
      svg.releasePointerCapture(event.pointerId);
    } catch {
      // best-effort
    }
    dragRef.current = null;
    setDraft(null);
    if (!didDragRef.current) {
      // A press that did not move is a click. Selection is handled HERE (not via a
      // DOM `onClick`) because a frame's pointerdown sets pointer capture on the
      // SVG, which redirects the subsequent `click` event to the SVG rather than
      // the frame — so an onClick on the frame would never fire.
      if (finished.mode === "move") {
        onSelect(finished.frameId);
      }
      return;
    }
    if (finished.mode === "create") {
      setFormError(null);
      setProfile(PROFILE_DEFAULTS);
      setNewFrame({ pxRect: normRect(finished.start, finished.cur) });
      return;
    }
    // Move: translate the frame's rendered rect by the drag delta, invert to mm,
    // and PATCH. Physical dimensions are preserved from the stored frame — a move
    // must never resize (the rendered rect uses project()'s data-dependent scale,
    // not dragToPlacement's; only the repositioned origin is taken from the drag).
    const dx = finished.cur.x - finished.start.x;
    const dy = finished.cur.y - finished.start.y;
    const moved = {
      x: finished.rect.x + dx,
      y: finished.rect.y + dy,
      w: finished.rect.w,
      h: finished.rect.h,
    };
    const placement = dragToPlacement(moved, VIEWPORT, surfaceId);
    const frame = framesById.get(finished.frameId);
    mutate(() =>
      moveFrame(finished.frameId, {
        surface_id: placement.surface_id,
        x_mm: placement.x_mm,
        y_mm: placement.y_mm,
        width_mm: frame?.width_mm ?? placement.width_mm,
        height_mm: frame?.height_mm ?? placement.height_mm,
      }),
    ).catch(() => {});
  };

  const submitNewFrame = (event) => {
    event.preventDefault();
    if (newFrame == null || surfaceId == null) {
      return;
    }
    const placement = dragToPlacement(newFrame.pxRect, VIEWPORT, surfaceId);
    const widthPx = Number(profile.width_px);
    const heightPx = Number(profile.height_px);
    const diagonal = Number(profile.diagonal_inches);
    if (!(widthPx > 0 && heightPx > 0 && diagonal > 0)) {
      setFormError("Profile dimensions must be positive.");
      return;
    }
    // Reject an incoherent profile client-side (design §J2: inline reason, no
    // request) — the server enforces the same guard as a 422 backstop.
    if (!orientationCoherent(placement.width_mm, placement.height_mm, widthPx, heightPx)) {
      setFormError("Display profile must match the frame's orientation.");
      return;
    }
    mutate(() =>
      createFrame(placement, {
        width_px: widthPx,
        height_px: heightPx,
        diagonal_inches: diagonal,
        video: profile.video,
      }),
    )
      .then((result) => {
        if (result.ok) {
          setNewFrame(null);
          setFormError(null);
        } else {
          setFormError("Could not create the frame — check the profile.");
        }
      })
      .catch(() => setFormError("Could not create the frame."));
  };

  const onDelete = () => {
    if (selection == null) {
      return;
    }
    setDeleteError(null);
    mutate(() => deleteFrame(selection))
      .then((result) => {
        if (result.ok) {
          onDeleted?.();
        } else {
          // Surface the design §9a guard wording verbatim (unbind / finish the Run).
          setDeleteError(result.message);
        }
      })
      .catch(() => setDeleteError("Could not delete the frame."));
  };

  const draftRect = draft;

  return (
    <section
      className="plan"
      role="group"
      aria-label={`Wall plan for surface ${surfaceId}`}
    >
      <svg
        ref={svgRef}
        className="plan__svg"
        viewBox={`0 0 ${VIEWPORT.width} ${VIEWPORT.height}`}
        width={VIEWPORT.width}
        height={VIEWPORT.height}
        preserveAspectRatio="xMinYMin meet"
        onPointerDown={beginCreate}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      >
        {placed.map(({ id, rect }) => {
          const selected = selection === id;
          const now = nowShowing(snapshot?.runtime, id);
          const connected = connectivity(snapshot, id);
          const frame = framesById.get(id);
          const calibrationValid = frame?.calibration_valid === true;
          return (
            <React.Fragment key={id}>
              <g
                className="plan__frame"
                role="button"
                tabIndex={0}
                aria-label={`Frame ${id}`}
                aria-pressed={selected}
                onPointerDown={(event) => beginMove(event, id, rect)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(id);
                  }
                }}
              >
                <rect
                  x={rect.x}
                  y={rect.y}
                  width={rect.w}
                  height={rect.h}
                  className={selected ? "plan__rect plan__rect--selected" : "plan__rect"}
                />
                <text
                  x={rect.x + rect.w / 2}
                  y={rect.y + rect.h / 2}
                  className="plan__label"
                  textAnchor="middle"
                  dominantBaseline="middle"
                >
                  {id}
                </text>
              </g>
              <g className="plan__status" role="group" aria-label={`Frame ${id} status`}>
                <circle
                  cx={rect.x + 10}
                  cy={rect.y + 12}
                  r={5}
                  className={`plan__dot plan__dot--${connected}`}
                  role="img"
                  aria-label={CONNECTIVITY_LABEL[connected]}
                />
                {now === null ? (
                  <text x={rect.x + 22} y={rect.y + 16} className="plan__chip plan__chip--idle">
                    Not scheduled
                  </text>
                ) : (
                  <>
                    <text x={rect.x + 22} y={rect.y + 16} className="plan__chip">
                      {`Scheduled: ${now.scene_id}`}
                    </text>
                    <text x={rect.x + 22} y={rect.y + 30} className="plan__phase">
                      {`Phase: ${now.phase}`}
                    </text>
                  </>
                )}
                <text
                  x={rect.x + 22}
                  y={rect.y + 44}
                  className={`plan__badge plan__badge--${calibrationValid ? "valid" : "invalid"}`}
                >
                  {calibrationValid ? "Calibration valid" : "Calibration invalid"}
                </text>
              </g>
            </React.Fragment>
          );
        })}
        {draftRect != null && (
          <rect
            className="plan__draft"
            x={draftRect.x}
            y={draftRect.y}
            width={draftRect.w}
            height={draftRect.h}
          />
        )}
      </svg>

      {newFrame != null && (
        <form className="plan__new-frame" aria-label="New frame" onSubmit={submitNewFrame}>
          <h3 className="plan__new-frame-title">New frame</h3>
          <label className="plan__new-frame-field">
            Display width (px)
            <input
              type="number"
              min="1"
              value={profile.width_px}
              onChange={(event) => setProfile({ ...profile, width_px: event.target.value })}
            />
          </label>
          <label className="plan__new-frame-field">
            Display height (px)
            <input
              type="number"
              min="1"
              value={profile.height_px}
              onChange={(event) => setProfile({ ...profile, height_px: event.target.value })}
            />
          </label>
          <label className="plan__new-frame-field">
            Diagonal (inches)
            <input
              type="number"
              min="1"
              step="any"
              value={profile.diagonal_inches}
              onChange={(event) => setProfile({ ...profile, diagonal_inches: event.target.value })}
            />
          </label>
          <label className="plan__new-frame-field plan__new-frame-field--check">
            <input
              type="checkbox"
              checked={profile.video}
              onChange={(event) => setProfile({ ...profile, video: event.target.checked })}
            />
            Video capable
          </label>
          <div className="plan__new-frame-actions">
            <button type="submit">Create frame</button>
            <button type="button" onClick={() => setNewFrame(null)}>
              Cancel
            </button>
          </div>
          {formError != null && (
            <p className="plan__new-frame-error" role="alert">
              {formError}
            </p>
          )}
        </form>
      )}

      {selection != null && (
        <div
          className="plan__selection"
          role="group"
          aria-label={`Selected frame ${selection}`}
        >
          <button type="button" className="plan__delete" onClick={onDelete}>
            {`Delete frame ${selection}`}
          </button>
          {deleteError != null && (
            <p className="plan__delete-error" role="alert">
              {deleteError}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
