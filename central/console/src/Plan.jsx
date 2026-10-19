import React, { useId, useRef, useState } from "react";

import { FRAME_ID_PATTERN } from "./frameIds.js";
import { factText } from "./facts.js";
import { frameHealth } from "./health.js";
import { isBound, plannedFor } from "./join.js";
import { pointToPlacement, project, unsizedOutlinePx } from "./projection.js";

/**
 * Per-Surface plan (Bead 1 read-only tracer; Bead 2 status chips; Bead 10 spatial
 * editing — drag-to-create + drag-to-move; console DDD §61: read-only unless in Edit layout).
 *
 * READ-ONLY BY DEFAULT (console DDD G3). Without `edit` the plan only selects: a click or
 * Enter/Space on a tile calls `onSelect`, and a drag does nothing. This module imports no
 * write module (framesApi writes, ConfirmAction, useMutate; tests/test_console_wall_layout.py
 * scans it): every write arrives as an `edit` handler from LayoutEditor.jsx, the Wall's
 * Edit layout mode (`#/wall/layout`), which owns them.
 *
 * Renders the selected Surface's placed frames as hand-coded SVG `<rect>`
 * elements from where each hangs (`x_mm/y_mm`, its top-left) at its size on the wall (Central's
 * `size_mm`: its Position box's share of its Display's own size), scaled to fit the viewport by
 * the pure `project()` helper. A Frame whose size is not known yet (bound to nothing, or on a
 * Display that states none) is a dashed `UNSIZED_FRAME_MM` outline labelled "size not known yet"
 * (frame box run decision 6). Each frame is selectable — clicking
 * (or Enter/Space on the focused frame) calls `onSelect(frameId)`; `selection` is
 * the currently-selected `frameId|null`. Every frame carries an accessible name
 * that embeds its frame id, so tests locate frames by identity via role/text —
 * never by coordinates (design §1c, tracer testing philosophy).
 *
 * Alongside each drawn frame the plan renders a status readout (Bead 2; console DDD §34):
 * the frame's `planned` fact ("On top: <scene_id> · <origin> (Central's Runs; …)", from
 * join.js `plannedFor`), its full text as the line's accessible name, with "Ending
 * (outro)" beneath it only in the outro phase, and the frame's health — a severity dot plus its short tile label (the fact
 * without its age, so it fits the tile) whose accessible name is the full label
 * — from the one classifier, `frameHealth` (health.js). The readout is clipped
 * to the tile's rect, so no text ever spills over a neighbouring tile. The planned fact is Central's projection, never
 * confirmed playback — the word "LIVE" is deliberately absent (design §6a).
 *
 * SPATIAL EDITING (Bead 10, design J3/§9a; frame box run fb-14): a click or a drag on EMPTY
 * canvas places a new Frame's top-left (a drag shows the not-yet-known outline under the pointer,
 * Plane B, held as component-local drag state) and, on release, opens a minimal new-frame form to
 * capture the Frame id (slice 2 §8: readable, checked as you type against FRAME_ID_PATTERN, never
 * generated), where it hangs and Shows videos; submitting calls `edit.createFrame`. Its shape and
 * size are never asked: they come from its Position box on its Display. A pointer drag that
 * starts ON an existing frame moves its top-left via `edit.placeFrame` (`PATCH`, last-write-wins,
 * no token — §9a). LayoutEditor runs both through the shared `useMutate()` hook so the plan
 * corrects from the next Plane A snapshot. A press on a frame that does not move beyond a small
 * threshold stays a plain click (selection).
 *
 * Origin-stacked / geometry-less frames are NOT drawn here; they belong to the
 * Unplaced tray (see UnplacedTray.jsx).
 *
 * DELETE (slice 2 §7): `edit.deleteFrame` opens the one confirmation dialog, which
 * LayoutEditor owns; `edit.status` (its status line and dialog) renders inside the plan
 * region, so after a delete the plan region takes focus and its status line says what
 * happened. `regionRef` (optional) is attached to the plan region so the Unplaced tray
 * can move focus here after its own delete.
 *
 * @typedef {{createFrame: (id: string, placement: object, video: boolean) => Promise<{ok: boolean, error?: string}>,
 *            placeFrame: (frameId: string, placement: object) => Promise<{ok: boolean, error?: string}>,
 *            takeTrayDrag: () => string|null,
 *            dropFromTray: (frameId: string, point: object, viewport: object, surfaceId: string) => void,
 *            deleteFrame: (event: {currentTarget: Element}, frameId: string) => void,
 *            status: React.ReactNode}} PlanEdit
 *   `takeTrayDrag` returns, and clears, the Unplaced tray entry being dragged (or null).
 *
 * @param {{snapshot: object|null, surfaceId: string|null,
 *          selection: string|null, onSelect: (frameId: string) => void,
 *          regionRef?: React.MutableRefObject<HTMLElement|null>, edit?: PlanEdit|null}} props
 */
const VIEWPORT = { width: 960, height: 600 };

// Movement (viewBox px) a press must exceed before it counts as a drag rather
// than a click. Below this, a press-release on a frame selects it.
const DRAG_THRESHOLD = 6;

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

/**
 * The outline a new Frame is drawn as while it is placed: the not-yet-known size, its top-left
 * the top-left of the press and the pointer (a click: the press itself).
 */
function newFrameOutline(start, cur) {
  const { x, y } = normRect(start, cur);
  return { x, y, ...UNSIZED_PX };
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

/** The not-yet-known outline in viewBox px: what a new Frame is drawn as while it is placed. */
const UNSIZED_PX = unsizedOutlinePx(VIEWPORT);

/** The placement form's starting values (a Frame placed with measurements starts here). */
const PLACEMENT_DEFAULTS = { x_mm: "1", y_mm: "0" };

const FRAME_ID_HINT = "Letters, digits, -, _ or .; up to 96; cannot be changed later.";

/** Why a typed Frame id cannot be used, or null when it can. */
function frameIdProblem(id) {
  if (id === "") {
    return "Enter a frame id.";
  }
  return FRAME_ID_PATTERN.test(id) ? null : `Frame id: ${FRAME_ID_HINT}`;
}

export function Plan({ snapshot, surfaceId, selection, onSelect, regionRef, edit = null }) {
  const svgRef = useRef(null);
  const ownRegionRef = useRef(/** @type {HTMLElement|null} */ (null));
  const planRef = regionRef ?? ownRegionRef;
  // Each tile's status readout is clipped to its rect, so no text leaves the tile.
  const clipPrefix = "plan-clip" + useId().replace(/[^A-Za-z0-9_-]/g, "");
  // Plane B: the LIVE in-progress drag (create or move). Held in a ref, not state,
  // because a full pointerdown->move->up sequence can fire before React re-renders
  // — the move/up handlers must read the drag synchronously (cf. tryingRef in
  // liveAdjustment.js). `draft` mirrors it purely to render the in-progress rectangle.
  const dragRef = useRef(/** @type {object|null} */ (null));
  const [draft, setDraft] = useState(/** @type {object|null} */ (null));
  // The open placement form: a new Frame (its id, where it hangs, Shows videos) or a Frame's move.
  const [placementForm, setPlacementForm] = useState(/** @type {{mode: "create"|"edit", frameId?: string}|null} */ (null));
  const [placement, setPlacement] = useState(PLACEMENT_DEFAULTS);
  const [video, setVideo] = useState(true);
  const [frameId, setFrameId] = useState("");
  const hintId = useId();
  const [formError, setFormError] = useState(/** @type {string|null} */ (null));
  // True once the current press has moved past the threshold — read by a frame's
  // onClick so a drag-move is not also treated as a selection.
  const didDragRef = useRef(false);

  const frames = snapshot?.inventory?.frames ?? [];
  const framesById = new Map(frames.map((frame) => [frame.id, frame]));
  const { placed } = project(frames, surfaceId, VIEWPORT);

  const beginCreate = (event) => {
    if (surfaceId == null || placementForm != null) {
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
    setDraft(newFrameOutline(start, start));
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
        ? newFrameOutline(drag.start, cur)
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
    const trayFrameId = edit.takeTrayDrag();
    if (trayFrameId != null) {
      if (surfaceId != null) {
        const pt = toViewbox(svgRef.current, event);
        edit.dropFromTray(trayFrameId, pt, VIEWPORT, surfaceId);
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
    if (finished.mode === "create") {
      // A click or a drag on empty canvas: the new Frame hangs at the top-left of press and release.
      const corner = newFrameOutline(finished.start, finished.cur);
      const placed = pointToPlacement(corner, VIEWPORT, surfaceId);
      openCreate({ x_mm: String(placed.x_mm), y_mm: String(placed.y_mm) });
      return;
    }
    if (!didDragRef.current) {
      // A press on a frame that did not move is a click. Selection is handled HERE (not
      // via a DOM `onClick`) because a frame's pointerdown sets pointer capture on the
      // SVG, which redirects the subsequent `click` event to the SVG rather than the
      // frame — so an onClick on the frame would never fire.
      onSelect(finished.frameId);
      return;
    }
    // Move: translate the frame's rendered top-left by the drag delta, invert to mm, and
    // PATCH where it hangs. Its size is never sent: it comes from its Display.
    edit.placeFrame(finished.frameId, pointToPlacement({
      x: finished.rect.x + finished.cur.x - finished.start.x,
      y: finished.rect.y + finished.cur.y - finished.start.y,
    }, VIEWPORT, surfaceId)).catch(() => {});
  };

  const openCreate = (where) => {
    setFormError(null);
    setVideo(true);
    setFrameId("");
    setPlacement(where);
    setPlacementForm({ mode: "create" });
  };

  const openMeasuredCreate = () => openCreate(PLACEMENT_DEFAULTS);

  const openPlacementEdit = () => {
    const frame = framesById.get(selection);
    if (frame == null) return;
    setFormError(null);
    setPlacement({ x_mm: String(frame.x_mm), y_mm: String(frame.y_mm) });
    setPlacementForm({ mode: "edit", frameId: frame.id });
  };

  const submitPlacement = (event) => {
    event.preventDefault();
    if (edit === null || placementForm == null || surfaceId == null) {
      return;
    }
    const numeric = Object.fromEntries(Object.entries(placement).map(([key, value]) => [key, Number(value)]));
    if (Object.values(placement).some((value) => String(value).trim() === "") ||
        Object.values(numeric).some((value) => !Number.isFinite(value))) {
      setFormError("Enter finite numbers for where the frame hangs."); return;
    }
    if (numeric.x_mm === 0 && numeric.y_mm === 0) {
      setFormError("Position (0, 0) is reserved for Unplaced frames. Choose a different x or y."); return;
    }
    if (placementForm.mode === "create") {
      const idProblem = frameIdProblem(frameId);
      if (idProblem !== null) { setFormError(idProblem); return; }
    }
    const framePlacement = { surface_id: surfaceId, x_mm: numeric.x_mm, y_mm: numeric.y_mm };
    if (placementForm.mode === "edit") {
      edit.placeFrame(placementForm.frameId, framePlacement).then((result) => {
        if (result.ok) { setPlacementForm(null); setFormError(null); }
        else setFormError(`Could not save placement: ${result.error}`);
      }).catch((error) => setFormError(`Could not save placement: ${error?.message ?? "server error"}`));
      return;
    }
    const id = frameId;
    edit.createFrame(id, framePlacement, video)
      .then((result) => {
        if (result.ok) {
          setPlacementForm(null);
          setFormError(null);
        } else if (result.error === "frame_exists") {
          setFormError(`A frame named ${id} already exists.`);
        } else {
          setFormError(`Could not create the frame: ${result.error}`);
        }
      })
      .catch((error) => setFormError(`Could not create the frame: ${error?.message ?? "server error"}`));
  };

  const onDelete = (event) => {
    if (selection == null) {
      return;
    }
    edit.deleteFrame(event, selection);
  };

  const draftRect = draft;

  return (
    <section
      ref={planRef}
      className="plan"
      role="group"
      aria-label={`Wall plan for surface ${surfaceId}`}
      tabIndex={-1}
    >
      <svg
        ref={svgRef}
        className="plan__svg"
        viewBox={`0 0 ${VIEWPORT.width} ${VIEWPORT.height}`}
        width={VIEWPORT.width}
        height={VIEWPORT.height}
        preserveAspectRatio="xMinYMin meet"
        {...(edit === null ? {} : {
          onPointerDown: beginCreate,
          onPointerMove,
          onPointerUp,
        })}
      >
        {placed.length === 0 && draftRect == null && (
          <text
            x={VIEWPORT.width / 2}
            y={VIEWPORT.height / 2}
            textAnchor="middle"
            className="plan__empty-hint"
          >
            {edit === null ? "No Frames placed on this Surface" : "Click or drag on this plan to place a Frame"}
          </text>
        )}
        {placed.map(({ id, rect, sized }, index) => {
          const selected = selection === id;
          const planned = plannedFor(snapshot?.runtime, id, isBound(framesById.get(id)));
          const plannedText = factText(planned.fact);
          const health = frameHealth(snapshot, id);
          return (
            <React.Fragment key={id}>
              <g
                className="plan__frame"
                role="button"
                tabIndex={0}
                aria-label={`Frame ${id}`}
                aria-pressed={selected}
                {...(edit === null
                  ? { onClick: () => onSelect(id) }
                  : { onPointerDown: (event) => beginMove(event, id, rect) })}
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
                  className={["plan__rect", selected ? "plan__rect--selected" : null,
                    sized ? null : "plan__rect--unsized"].filter(Boolean).join(" ")}
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
                {!sized && (
                  <text
                    x={rect.x + rect.w / 2}
                    y={rect.y + rect.h / 2 + 20}
                    className="plan__label plan__label--note"
                    textAnchor="middle"
                    dominantBaseline="middle"
                  >
                    size not known yet
                  </text>
                )}
              </g>
              <clipPath id={`${clipPrefix}-${index}`}>
                <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} />
              </clipPath>
              <g
                className="plan__status"
                role="group"
                aria-label={`Frame ${id} status`}
                clipPath={`url(#${clipPrefix}-${index})`}
              >
                <circle
                  cx={rect.x + 10}
                  cy={rect.y + 12}
                  r={5}
                  className={`plan__dot health--${health.severity}`}
                  aria-hidden="true"
                />
                <text
                  x={rect.x + 22}
                  y={rect.y + 16}
                  className={`plan__chip fact--${planned.fact.kind}${planned.sceneId === null ? " plan__chip--idle" : ""}`}
                  role="img"
                  aria-label={plannedText}
                >
                  {plannedText}
                </text>
                {planned.phase === "outro" && (
                  <text x={rect.x + 22} y={rect.y + 30} className="plan__phase">
                    Ending (outro)
                  </text>
                )}
                <text
                  x={rect.x + 22}
                  y={rect.y + 44}
                  className={`plan__health health--${health.severity}`}
                  role="img"
                  aria-label={health.label}
                >
                  {health.tileLabel}
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

      {edit !== null && (
        <button type="button" disabled={surfaceId == null || placementForm != null} onClick={openMeasuredCreate}>
          Add frame with measurements
        </button>
      )}

      {edit !== null && placementForm != null && (
        <form className="plan__new-frame" aria-label={placementForm.mode === "create" ? "New frame" : `Place frame ${placementForm.frameId}`} onSubmit={submitPlacement}>
          <h3 className="plan__new-frame-title">{placementForm.mode === "create" ? "New frame" : `Edit placement for ${placementForm.frameId}`}</h3>
          {placementForm.mode === "create" && <>
          <label className="plan__new-frame-field">
            Frame id
            <input
              type="text"
              required
              autoFocus
              value={frameId}
              aria-describedby={hintId}
              aria-invalid={frameId !== "" && frameIdProblem(frameId) !== null}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              onChange={(event) => {
                setFrameId(event.target.value);
                setFormError(null);
              }}
            />
          </label>
          <p id={hintId} className="plan__new-frame-hint">
            {frameId !== "" && frameIdProblem(frameId) !== null
              ? `Not usable. ${FRAME_ID_HINT}`
              : FRAME_ID_HINT}
          </p>
          </>}
          <label className="plan__new-frame-field">X position (mm)
            <input required autoFocus={placementForm.mode === "edit"} type="number" step="any" value={placement.x_mm} onChange={(event) => setPlacement({ ...placement, x_mm: event.target.value })} />
          </label>
          <label className="plan__new-frame-field">Y position (mm)
            <input required type="number" step="any" value={placement.y_mm} onChange={(event) => setPlacement({ ...placement, y_mm: event.target.value })} />
          </label>
          {placementForm.mode === "create" && <>
          <label className="plan__new-frame-field plan__new-frame-field--check">
            <input
              type="checkbox"
              checked={video}
              onChange={(event) => setVideo(event.target.checked)}
            />
            Shows videos
          </label>
          <p className="plan__new-frame-hint">
            Its size comes from its display once it is connected; set its shape on the Position tab.
          </p>
          </>}
          <div className="plan__new-frame-actions">
            <button type="submit">{placementForm.mode === "create" ? "Create frame" : "Save placement"}</button>
            <button type="button" onClick={() => { setPlacementForm(null); setFormError(null); }}>
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

      {edit !== null && selection != null && (
        <div
          className="plan__selection"
          role="group"
          aria-label={`Selected frame ${selection}`}
        >
          <button type="button" onClick={openPlacementEdit} disabled={placementForm != null}>
            {`Edit placement for ${selection}`}
          </button>
          <button type="button" className="plan__delete" onClick={onDelete}>
            {`Delete frame ${selection}`}
          </button>
        </div>
      )}
      {edit?.status}
    </section>
  );
}
