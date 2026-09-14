import React, { useRef, useState } from "react";

import { boundOutput, connectivity } from "./join.js";
import { derive } from "./capability.js";
import { GatedArea } from "./GatedArea.jsx";
import { useDraft } from "./useDraft.js";
import { cornerHandles, cropHandles, toNormalized } from "./projection.js";

// The calibration editor draws normalized output space `[0, 1]` onto a square of
// SIZE px, inset by PAD so the four full-frame corner handles sit comfortably
// inside the SVG viewport rather than clipped on its edges.
const SIZE = 300;
const PAD = 16;
const VIEW = SIZE + PAD * 2;

const DEFAULT_CORNERS = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];

/** Value-equality of a draft against the committed calibration baseline. */
function matchesCommitted(trying, calibration) {
  const corners = Array.isArray(calibration.corners) ? calibration.corners : DEFAULT_CORNERS;
  const crop = Array.isArray(calibration.crop) ? calibration.crop : [0, 0, 1, 1];
  return (
    trying.gain === (calibration.gain ?? 1) &&
    trying.rotation === (calibration.rotation ?? 0) &&
    trying.crop.every((value, index) => value === crop[index]) &&
    trying.corners.every(
      (point, index) => point[0] === corners[index][0] && point[1] === corners[index][1],
    )
  );
}

/**
 * Commissioning facet — read-only readback (Bead 4) + calibration draft editing
 * (Bead 7, design §J2/§4a/§6b).
 *
 * The Display↔Frame hardware relationship. The upper sections show four honest
 * READ-ONLY things (committed calibration, Frame facts, live Display readback,
 * bound equipment) and gate the hardware areas off; see the section comments
 * below and design §7.
 *
 * The lower **Adjust calibration** section is Plane B (design §4a): the operator
 * directly manipulates corner/crop handles and the SDR gain, all writing to a
 * component-local {@link useDraft} draft that a snapshot refresh NEVER
 * overwrites. Every geometry edit is validated by the SAME convex test + `1e-6`
 * epsilon + winding as the server (convex.js): a folded/thin quad or empty crop
 * snaps the handle back with an inline message and sends NO request. Preview and
 * commit (network writes) are Bead 8; this bead performs no writes.
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function Commissioning({ snapshot, frameId }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  const calibration = frame?.calibration ?? {};

  // Plane B draft, seeded from the committed calibration and refresh-proof. This
  // hook is called unconditionally (before the early return) to keep hook order
  // stable; when the frame is absent the draft simply seeds from defaults.
  const { trying, updateHandles } = useDraft(frameId, calibration);
  const [error, setError] = useState(/** @type {string|null} */ (null));
  const svgRef = useRef(/** @type {SVGSVGElement|null} */ (null));
  const dragRef = useRef(/** @type {{kind: string, index?: number}|null} */ (null));

  if (!frame) {
    return (
      <div className="facet facet--commissioning">
        <h3 className="facet__title">Commissioning</h3>
        <p className="facet__empty">This frame is no longer in the inventory.</p>
      </div>
    );
  }

  const profile = frame.profile ?? {};
  const output = boundOutput(snapshot, frameId);
  const observation = output?.observation ?? null;
  const status = connectivity(snapshot, frameId);
  const bound = frame.player_id != null && frame.output_id != null;

  const cornerText = Array.isArray(calibration.corners)
    ? calibration.corners.map((point) => `(${point[0]}, ${point[1]})`).join(" ")
    : "—";
  const cropText = Array.isArray(calibration.crop) ? calibration.crop.join(", ") : "—";

  // The would-be live hardware controls. They are the CHILDREN of GatedArea, so
  // they exist in the DOM ONLY when derive() returns "derived-true" from a real
  // wired path — which T0 never does. Do not lift these out of the gate.
  const colorState = derive("photometric_calibration", snapshot);
  const powerState = derive("display_command", snapshot);

  const dirty = !matchesCommitted(trying, calibration);

  // Every draft edit routes through updateHandles; an invalid geometry patch is
  // rejected (the draft is unchanged, so the control snaps back) and its reason
  // is surfaced inline. No branch here issues a network request.
  const apply = (result) => {
    setError(result.valid ? null : result.reason);
  };

  const setCorner = (index, nx, ny) => {
    const corners = trying.corners.map((point) => [point[0], point[1]]);
    corners[index] = [nx, ny];
    apply(updateHandles({ corners }));
  };

  const setCornerAxis = (index, axis, rawValue) => {
    const value = Number(rawValue);
    if (Number.isNaN(value)) {
      return;
    }
    const nx = axis === 0 ? value : trying.corners[index][0];
    const ny = axis === 1 ? value : trying.corners[index][1];
    setCorner(index, nx, ny);
  };

  const setCropEdge = (edge, rawValue) => {
    const value = Number(rawValue);
    if (Number.isNaN(value)) {
      return;
    }
    const crop = [...trying.crop];
    crop[edge] = value;
    apply(updateHandles({ crop }));
  };

  const pointerNormalized = (event) => {
    const rect = svgRef.current.getBoundingClientRect();
    return toNormalized(event.clientX - rect.left - PAD, event.clientY - rect.top - PAD, SIZE);
  };

  const onHandleDown = (event, kind, index) => {
    dragRef.current = { kind, index };
  };

  const onPointerMove = (event) => {
    const drag = dragRef.current;
    if (!drag) {
      return;
    }
    const [nx, ny] = pointerNormalized(event);
    if (drag.kind === "corner") {
      setCorner(drag.index, nx, ny);
    } else if (drag.kind === "crop-tl") {
      apply(updateHandles({ crop: [nx, ny, trying.crop[2], trying.crop[3]] }));
    } else if (drag.kind === "crop-br") {
      apply(updateHandles({ crop: [trying.crop[0], trying.crop[1], nx, ny] }));
    }
  };

  const endDrag = () => {
    dragRef.current = null;
  };

  const handles = cornerHandles(trying.corners, SIZE);
  const crop = cropHandles(trying.crop, SIZE);
  const polygonPoints = handles.map((handle) => `${handle.x},${handle.y}`).join(" ");

  return (
    <div className="facet facet--commissioning">
      <h3 className="facet__title">Commissioning</h3>

      <section className="facet__section" role="group" aria-label="Committed calibration">
        <h4 className="facet__subtitle">Committed calibration</h4>
        <dl className="facet__fields">
          <div className="facet__field">
            <dt>SDR gain</dt>
            <dd>{calibration.gain ?? "—"}</dd>
          </div>
          <div className="facet__field">
            <dt>Rotation</dt>
            <dd>{`${calibration.rotation ?? 0}°`}</dd>
          </div>
          <div className="facet__field">
            <dt>Corners</dt>
            <dd>{cornerText}</dd>
          </div>
          <div className="facet__field">
            <dt>Crop</dt>
            <dd>{cropText}</dd>
          </div>
        </dl>
      </section>

      <section className="facet__section facet__section--editor" role="group" aria-label="Adjust calibration">
        <h4 className="facet__subtitle">Adjust calibration</h4>
        <p className="facet__note">
          Drag the corner and crop handles or edit the values; changes stay a
          local draft until you preview or commit.
        </p>

        <svg
          ref={svgRef}
          className="calib__svg"
          role="img"
          aria-label="Calibration editor"
          width={VIEW}
          height={VIEW}
          viewBox={`0 0 ${VIEW} ${VIEW}`}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerLeave={endDrag}
        >
          <g transform={`translate(${PAD}, ${PAD})`}>
            <rect className="calib__frame" x={0} y={0} width={SIZE} height={SIZE} />
            <rect
              className="calib__crop"
              x={crop.topLeft.x}
              y={crop.topLeft.y}
              width={crop.bottomRight.x - crop.topLeft.x}
              height={crop.bottomRight.y - crop.topLeft.y}
            />
            <polygon className="calib__quad" points={polygonPoints} />
            {handles.map((handle) => (
              <circle
                key={`corner-${handle.index}`}
                className="calib__handle calib__handle--corner"
                cx={handle.x}
                cy={handle.y}
                r={9}
                onPointerDown={(event) => onHandleDown(event, "corner", handle.index)}
              />
            ))}
            <circle
              className="calib__handle calib__handle--crop"
              cx={crop.topLeft.x}
              cy={crop.topLeft.y}
              r={7}
              onPointerDown={(event) => onHandleDown(event, "crop-tl")}
            />
            <circle
              className="calib__handle calib__handle--crop"
              cx={crop.bottomRight.x}
              cy={crop.bottomRight.y}
              r={7}
              onPointerDown={(event) => onHandleDown(event, "crop-br")}
            />
          </g>
        </svg>

        {error !== null ? (
          <p className="facet__error" role="alert">
            {error}
          </p>
        ) : null}

        <p className="facet__draft-status" role="status">
          {dirty ? "Unsaved draft changes" : "Draft matches committed"}
        </p>

        <div className="calib__controls">
          <label className="calib__control">
            SDR gain (draft)
            <input
              type="number"
              step="0.1"
              min="0"
              max="2"
              value={trying.gain}
              onChange={(event) => apply(updateHandles({ gain: Number(event.target.value) }))}
            />
          </label>
          <label className="calib__control">
            Rotation (draft)
            <select
              value={trying.rotation}
              onChange={(event) => apply(updateHandles({ rotation: Number(event.target.value) }))}
            >
              {[0, 90, 180, 270].map((angle) => (
                <option key={angle} value={angle}>
                  {`${angle}°`}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="calib__controls" role="group" aria-label="Corner coordinates">
          {trying.corners.map((point, index) => (
            <React.Fragment key={`corner-input-${index}`}>
              <label className="calib__control">
                {`Corner ${index + 1} x`}
                <input
                  type="number"
                  step="0.01"
                  value={point[0]}
                  onChange={(event) => setCornerAxis(index, 0, event.target.value)}
                />
              </label>
              <label className="calib__control">
                {`Corner ${index + 1} y`}
                <input
                  type="number"
                  step="0.01"
                  value={point[1]}
                  onChange={(event) => setCornerAxis(index, 1, event.target.value)}
                />
              </label>
            </React.Fragment>
          ))}
        </div>

        <div className="calib__controls" role="group" aria-label="Crop rectangle">
          {["Crop left", "Crop top", "Crop right", "Crop bottom"].map((label, edge) => (
            <label className="calib__control" key={label}>
              {label}
              <input
                type="number"
                step="0.01"
                value={trying.crop[edge]}
                onChange={(event) => setCropEdge(edge, event.target.value)}
              />
            </label>
          ))}
        </div>
      </section>

      <section className="facet__section" role="group" aria-label="Frame facts">
        <h4 className="facet__subtitle">Frame facts</h4>
        <p className="facet__note">
          Operator-declared at frame creation; persist across a panel swap.
        </p>
        <dl className="facet__fields">
          <div className="facet__field">
            <dt>Pixel width</dt>
            <dd>{`${profile.width_px ?? "—"} px`}</dd>
          </div>
          <div className="facet__field">
            <dt>Pixel height</dt>
            <dd>{`${profile.height_px ?? "—"} px`}</dd>
          </div>
          <div className="facet__field">
            <dt>Diagonal</dt>
            <dd>{`${profile.diagonal_inches ?? "—"} in`}</dd>
          </div>
          <div className="facet__field">
            <dt>Video capable</dt>
            <dd>{profile.video ? "yes" : "no"}</dd>
          </div>
        </dl>
      </section>

      <section className="facet__section" role="group" aria-label="Live Display readback">
        <h4 className="facet__subtitle">Live Display readback</h4>
        {observation ? (
          <dl className="facet__fields">
            <div className="facet__field">
              <dt>Connected</dt>
              <dd>{observation.connected ? "Connected" : "Disconnected"}</dd>
            </div>
            <div className="facet__field">
              <dt>Output resolution</dt>
              <dd>{`${observation.width_px} × ${observation.height_px}`}</dd>
            </div>
          </dl>
        ) : (
          <p className="facet__empty">
            {status === "unbound"
              ? "No Display bound — bind a Player output first."
              : "No live readback from the bound output."}
          </p>
        )}
      </section>

      <section className="facet__section" role="group" aria-label="Display equipment">
        <h4 className="facet__subtitle">Display equipment</h4>
        {bound ? (
          <dl className="facet__fields">
            <div className="facet__field">
              <dt>Player</dt>
              <dd>{frame.player_id}</dd>
            </div>
            <div className="facet__field">
              <dt>Output</dt>
              <dd>{frame.output_id}</dd>
            </div>
          </dl>
        ) : (
          <p className="facet__empty">Unbound</p>
        )}
      </section>

      <section className="facet__section" role="group" aria-label="Panel color correction">
        <h4 className="facet__subtitle">Panel color correction</h4>
        <GatedArea state={colorState}>
          <button type="button" className="facet__hardware-control">
            Adjust panel color correction
          </button>
        </GatedArea>
      </section>

      <section className="facet__section" role="group" aria-label="Display power and parameters">
        <h4 className="facet__subtitle">Display power and parameters</h4>
        <GatedArea state={powerState}>
          <button type="button" className="facet__hardware-control">
            Set display power
          </button>
        </GatedArea>
      </section>
    </div>
  );
}
