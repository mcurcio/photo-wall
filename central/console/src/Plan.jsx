import React, { useId, useRef, useState } from "react";

import { FRAME_ID_PATTERN } from "./frameIds.js";
import { frameHealth } from "./health.js";
import { nowShowing } from "./join.js";
import { dragToPlacement, orientationCoherent, project } from "./projection.js";

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
 * elements from their committed `x_mm/y_mm/width_mm/height_mm`, scaled to fit the
 * viewport by the pure `project()` helper. Each frame is selectable — clicking
 * (or Enter/Space on the focused frame) calls `onSelect(frameId)`; `selection` is
 * the currently-selected `frameId|null`. Every frame carries an accessible name
 * that embeds its frame id, so tests locate frames by identity via role/text —
 * never by coordinates (design §1c, tracer testing philosophy).
 *
 * Alongside each drawn frame the plan renders a status readout (Bead 2): the
 * intended now-showing chip "Scheduled: <scene_id>" + phase (from `nowShowing`)
 * and the frame's health — a severity dot plus its short tile label (the fact
 * without its age, so it fits the tile) whose accessible name is the full label
 * — from the one classifier, `frameHealth` (health.js). The readout is clipped
 * to the tile's rect, so no text ever spills over a neighbouring tile. The chip asserts operator INTENT, never
 * confirmed playback — the word "LIVE" is deliberately absent (design §6a).
 *
 * SPATIAL EDITING (Bead 10, design J3/§9a): a pointer drag on EMPTY canvas draws
 * an in-progress rectangle (Plane B, held as component-local drag state) and, on
 * release, opens a minimal new-frame form to capture the Frame id (slice 2 §8:
 * readable, checked as you type against FRAME_ID_PATTERN, never generated) and
 * the Frame profile (`FrameProfile`); submitting calls `edit.createFrame`. A pointer drag
 * that starts ON an existing frame repositions it via `edit.moveFrame` (`PATCH`,
 * last-write-wins, no token — §9a). LayoutEditor runs both through the shared
 * `useMutate()` hook so the plan corrects from the next Plane A snapshot. A press
 * that does not move beyond a small threshold stays a plain click (selection).
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
 * @typedef {{createFrame: (id: string, placement: object, profile: object) => Promise<{ok: boolean, error?: string}>,
 *            moveFrame: (frameId: string, placement: object) => Promise<{ok: boolean, error?: string}>,
 *            takeTrayDrag: () => string|null,
 *            dropFromTray: (frameId: string, pxRect: object, viewport: object, surfaceId: string) => void,
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
  // useCalibration). `draft` mirrors it purely to render the in-progress rectangle.
  const dragRef = useRef(/** @type {object|null} */ (null));
  const [draft, setDraft] = useState(/** @type {object|null} */ (null));
  // The pending new-frame drag rect awaiting a profile from the form (px).
  const [placementForm, setPlacementForm] = useState(/** @type {{mode: "create"|"edit", frameId?: string}|null} */ (null));
  const [placement, setPlacement] = useState({ x_mm: "1", y_mm: "0", width_mm: "400", height_mm: "225" });
  const [profile, setProfile] = useState(PROFILE_DEFAULTS);
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
    const trayFrameId = edit.takeTrayDrag();
    if (trayFrameId != null) {
      if (surfaceId != null) {
        const pt = toViewbox(svgRef.current, event);
        edit.dropFromTray(trayFrameId, { x: pt.x, y: pt.y, w: 1, h: 1 }, VIEWPORT, surfaceId);
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
      setFrameId("");
      const measured = dragToPlacement(normRect(finished.start, finished.cur), VIEWPORT, surfaceId);
      setPlacement({ x_mm: String(measured.x_mm), y_mm: String(measured.y_mm), width_mm: String(measured.width_mm), height_mm: String(measured.height_mm) });
      setPlacementForm({ mode: "create" });
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
    edit.moveFrame(finished.frameId, {
      surface_id: placement.surface_id,
      x_mm: placement.x_mm,
      y_mm: placement.y_mm,
      width_mm: frame?.width_mm ?? placement.width_mm,
      height_mm: frame?.height_mm ?? placement.height_mm,
    }).catch(() => {});
  };

  const openMeasuredCreate = () => {
    setFormError(null);
    setProfile(PROFILE_DEFAULTS);
    setFrameId("");
    setPlacement({ x_mm: "1", y_mm: "0", width_mm: "400", height_mm: "225" });
    setPlacementForm({ mode: "create" });
  };

  const openPlacementEdit = () => {
    const frame = framesById.get(selection);
    if (frame == null) return;
    setFormError(null);
    setPlacement({ x_mm: String(frame.x_mm), y_mm: String(frame.y_mm), width_mm: String(frame.width_mm), height_mm: String(frame.height_mm) });
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
      setFormError("Enter finite numbers for all placement measurements."); return;
    }
    if (!(numeric.width_mm > 0 && numeric.height_mm > 0)) {
      setFormError("Frame width and height must be positive."); return;
    }
    if (numeric.x_mm === 0 && numeric.y_mm === 0) {
      setFormError("Position (0, 0) is reserved for Unplaced frames. Choose a different x or y."); return;
    }
    if (placementForm.mode === "create") {
      const idProblem = frameIdProblem(frameId);
      if (idProblem !== null) { setFormError(idProblem); return; }
    }
    const framePlacement = { surface_id: surfaceId, ...numeric };
    const widthPx = Number(profile.width_px);
    const heightPx = Number(profile.height_px);
    const diagonal = Number(profile.diagonal_inches);
    if (!(widthPx > 0 && heightPx > 0 && diagonal > 0)) {
      setFormError("Profile dimensions must be positive.");
      return;
    }
    // Reject an incoherent profile client-side (design §J2: inline reason, no
    // request) — the server enforces the same guard as a 422 backstop.
    const frame = placementForm.mode === "edit" ? framesById.get(placementForm.frameId) : null;
    const effectiveProfile = placementForm.mode === "edit" ? frame?.profile : null;
    const checkedWidth = placementForm.mode === "edit" ? Number(effectiveProfile?.width_px) : widthPx;
    const checkedHeight = placementForm.mode === "edit" ? Number(effectiveProfile?.height_px) : heightPx;
    if (!orientationCoherent(numeric.width_mm, numeric.height_mm, checkedWidth, checkedHeight)) {
      setFormError("Frame profile must match the frame's orientation.");
      return;
    }
    if (placementForm.mode === "edit") {
      edit.moveFrame(placementForm.frameId, framePlacement).then((result) => {
        if (result.ok) { setPlacementForm(null); setFormError(null); }
        else setFormError(`Could not save placement: ${result.error}`);
      }).catch((error) => setFormError(`Could not save placement: ${error?.message ?? "server error"}`));
      return;
    }
    const id = frameId;
    edit.createFrame(id, framePlacement, {
      width_px: widthPx,
      height_px: heightPx,
      diagonal_inches: diagonal,
      video: profile.video,
    })
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
            {edit === null ? "No Frames placed on this Surface" : "Drag on this plan to place a Frame"}
          </text>
        )}
        {placed.map(({ id, rect }, index) => {
          const selected = selection === id;
          const now = nowShowing(snapshot?.runtime, id);
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
          <label className="plan__new-frame-field">Frame width (mm)
            <input required type="number" min="0.000001" step="any" value={placement.width_mm} onChange={(event) => setPlacement({ ...placement, width_mm: event.target.value })} />
          </label>
          <label className="plan__new-frame-field">Frame height (mm)
            <input required type="number" min="0.000001" step="any" value={placement.height_mm} onChange={(event) => setPlacement({ ...placement, height_mm: event.target.value })} />
          </label>
          {placementForm.mode === "create" && <>
          <label className="plan__new-frame-field">
            Pixel width (px)
            <input
              type="number"
              min="1"
              value={profile.width_px}
              onChange={(event) => setProfile({ ...profile, width_px: event.target.value })}
            />
          </label>
          <label className="plan__new-frame-field">
            Pixel height (px)
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
