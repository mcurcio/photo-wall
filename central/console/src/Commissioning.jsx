import React, { useEffect, useRef, useState } from "react";

import { isBound } from "./health.js";
import { boundOutput } from "./join.js";
import { derive } from "./capability.js";
import { GatedArea } from "./GatedArea.jsx";
import { useCalibration } from "./useCalibration.js";
import { useDraft } from "./useDraft.js";
import { cornerHandles, cropHandles, toNormalized } from "./projection.js";
import { frameProfileProblem, updateFrameProfile } from "./framesApi.js";
import { useMutate } from "./useMutate.js";
import { formatRoute, sceneCreationRoute } from "./routes.js";
import { FRAME_ID_PATTERN } from "./frameIds.js";

// Operator-facing lease/conflict copy, verbatim from design §4b / J2 (the
// authoritative decision table). The countdown banner is display; the panel's
// actual expiry is server-authoritative and surfaced by the overtake poll.
const EXPIRED_MESSAGE = "Panel is back on committed. Re-preview to keep trying.";
const OVERTAKEN_MESSAGE =
  "Committed elsewhere / your preview was superseded — re-review.";
const CONFLICT_MESSAGES = {
  // Stale expected_revision → calibration_revision_conflict (design §4b).
  revision: "Another session changed this frame's calibration — reload and re-review.",
  // Stale expected_generation → binding_generation_conflict (design §4b).
  generation:
    "This Frame's binding changed — its display is no longer under your control; reload.",
  // preview/commit refused because the frame is no longer bound.
  unbound: "This Frame is no longer bound to a display — bind it before calibrating.",
  // A non-token failure (validation/network); never mapped to a token conflict,
  // and deliberately worded so it cannot be mistaken for a specific conflict.
  error: "Calibration request failed — reload and try again.",
};

/** The banner text for the current lease state, or null when there is none. */
function leaseBanner(status, conflict) {
  if (status === "expired") {
    return EXPIRED_MESSAGE;
  }
  if (status === "overtaken") {
    return OVERTAKEN_MESSAGE;
  }
  if (status === "conflict" && conflict) {
    return CONFLICT_MESSAGES[conflict] ?? CONFLICT_MESSAGES.error;
  }
  return null;
}

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
 * The Display↔Frame hardware relationship. The upper sections show committed
 * calibration, editable Frame profile facts, the Display as detected
 * at the last Player start, and bound equipment; they gate unavailable hardware areas; see the section comments
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
  const { trying, updateHandles, clearDraft } = useDraft(frameId, calibration);
  // Bead 8 (Plane B network writes): preview/commit/revert under the 30s lease,
  // the server-driven countdown, and the overtake/expiry/conflict states. The
  // draft (trying) is threaded in so preview/commit carry the operator's values.
  const { calibrate, countdown, status: leaseStatus } = useCalibration(frameId, trying);
  // The last conflict kind returned by an op; the poll-driven states (expired,
  // overtaken) are read from `status` and need no extra bookkeeping.
  const [conflict, setConflict] = useState(/** @type {string|null} */ (null));
  const [error, setError] = useState(/** @type {string|null} */ (null));
  const [profileDraft, setProfileDraft] = useState(/** @type {object|null} */ (null));
  const [profileGeneration, setProfileGeneration] = useState(/** @type {number|null} */ (null));
  const [profileError, setProfileError] = useState(/** @type {string|null} */ (null));
  const [profileStatus, setProfileStatus] = useState(/** @type {string|null} */ (null));
  const [profileSaving, setProfileSaving] = useState(false);
  const mutate = useMutate();
  const profileEditButtonRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const svgRef = useRef(/** @type {SVGSVGElement|null} */ (null));
  const dragRef = useRef(/** @type {{kind: string, index?: number}|null} */ (null));

  // Restore focus after React has committed the editor's removal. A queued
  // animation frame may be throttled while the operator tab is in the background.
  useEffect(() => {
    if (profileDraft == null && profileStatus != null) {
      profileEditButtonRef.current?.focus();
    }
  }, [profileDraft, profileStatus]);

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
  const bound = isBound(frame);
  const rotation = calibration.rotation ?? 0;
  const hasUsableResolutions = (dimensions) =>
    Number.isFinite(dimensions?.width_px) && dimensions.width_px > 0 &&
    Number.isFinite(dimensions?.height_px) && dimensions.height_px > 0;
  const quarterTurn = frame.calibration_valid === true && (rotation === 90 || rotation === 270);
  const reportedProfileMismatch =
    bound && observation?.connected === true &&
    hasUsableResolutions(profile) && hasUsableResolutions(observation) &&
    (profile.width_px !== (quarterTurn ? observation.height_px : observation.width_px) ||
      profile.height_px !== (quarterTurn ? observation.width_px : observation.height_px));

  const beginProfileEdit = () => {
    setProfileDraft({
      width_px: String(profile.width_px ?? ""),
      height_px: String(profile.height_px ?? ""),
      diagonal_inches: String(profile.diagonal_inches ?? ""),
      video: Boolean(profile.video),
    });
    setProfileGeneration(frame.generation ?? 0);
    setProfileError(null);
    setProfileStatus(null);
  };

  const saveProfile = async (event) => {
    event.preventDefault();
    if (profileDraft == null) return;
    const problem = frameProfileProblem(profileDraft, frame);
    if (problem) {
      setProfileError(problem);
      return;
    }
    setProfileSaving(true);
    setProfileError(null);
    const submitted = {
      width_px: Number(profileDraft.width_px),
      height_px: Number(profileDraft.height_px),
      diagonal_inches: Number(profileDraft.diagonal_inches),
      video: profileDraft.video,
    };
    try {
      const result = await mutate(() => updateFrameProfile(frameId, submitted, profileGeneration));
      if (result.ok) {
        setProfileDraft(null);
        setProfileStatus(result.changed
          ? "Display profile saved. Recalibrate this Frame before showing content."
          : "Display profile already matches; calibration was not changed.");
      } else if (result.code === "binding_generation_conflict") {
        setProfileError("This Frame's equipment changed while you were editing. Reload its facts before retrying.");
      } else if (result.code === "frame_bound") {
        setProfileError("Unbind this Frame before changing its display profile. Your draft is preserved.");
      } else if (result.code === "frame_in_use") {
        setProfileError("Finish or cancel the active Run targeting this Frame, then retry. Your draft is preserved.");
      } else if (result.code === "oriented_profile") {
        setProfileError("Display profile must match the frame's orientation. Your draft is preserved.");
      } else if (result.code === "unknown_frame") {
        setProfileError("This Frame no longer exists. Reload the wall to continue.");
      } else {
        setProfileError(`Could not save the display profile (${result.code}). Your draft is preserved.`);
      }
    } catch (failure) {
      setProfileError(`Could not save the display profile: ${failure?.message ?? "server error"}. Your draft is preserved.`);
    } finally {
      setProfileSaving(false);
    }
  };

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

  // Run a calibration op and reflect its result. A conflict return sets the
  // banner kind; a success clears it. The poll independently drives expired /
  // overtaken through `status`.
  //
  // A successful manual Revert also DISCARDS the local draft back to committed
  // (parity with the legacy page's single-field revert): Revert clears the panel
  // preview server-side AND returns the editable draft to committed, so the
  // operator is back on truth. (Lease EXPIRY is different — it keeps the trying
  // values for Re-preview; that path never runs clearDraft.)
  const runOp = async (op) => {
    const result = await calibrate(op);
    setConflict(result.ok ? null : result.conflict);
    if (op === "revert" && result.ok) {
      clearDraft();
    }
  };

  const banner = leaseBanner(leaseStatus, conflict);

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

      {bound && frame.calibration_valid === true && FRAME_ID_PATTERN.test(frameId) && (
        <section className="facet__section facet__section--content" role="group" aria-label="Choose content">
          <h4 className="facet__subtitle">Ready to choose content?</h4>
          <p className="facet__note">
            Make a Scene and choose this Frame explicitly. Saving the Scene does not start playback.
          </p>
          <a className="facet__cta" href={formatRoute(sceneCreationRoute(frameId))}>
            Choose content for this Frame
          </a>
        </section>
      )}
      {bound && frame.calibration_valid === true && !FRAME_ID_PATTERN.test(frameId) && (
        <p className="facet__note" role="status">
          This Frame is commissioned, but its id cannot be targeted by a Scene. Scene targets need an id of 96 characters or fewer without a colon.
        </p>
      )}

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

      <section
        className="facet__section facet__section--lease"
        role="group"
        aria-label="Preview and commit"
      >
        <h4 className="facet__subtitle">Preview and commit</h4>
        <p className="facet__note">
          Preview pushes the draft to the panel under a 30-second server lease;
          Commit saves it as a new revision. There is no auto-renew — on expiry
          the panel returns to committed and the draft is kept for Re-preview.
        </p>

        <div className="calib__actions">
          <button
            type="button"
            className="calib__action"
            disabled={!bound}
            onClick={() => runOp("preview")}
          >
            Preview
          </button>
          <button
            type="button"
            className="calib__action"
            disabled={!bound}
            onClick={() => runOp("commit")}
          >
            Commit
          </button>
          <button
            type="button"
            className="calib__action"
            onClick={() => runOp("revert")}
          >
            Revert
          </button>
          {leaseStatus === "expired" ? (
            <button
              type="button"
              className="calib__action calib__action--re-preview"
              disabled={!bound}
              onClick={() => runOp("preview")}
            >
              Re-preview
            </button>
          ) : null}
        </div>

        {leaseStatus === "previewing" && countdown !== null ? (
          <p
            className="calib__countdown"
            role="timer"
            aria-label="Preview lease countdown"
          >
            {`Previewing on the panel — lease expires in ${countdown}s.`}
          </p>
        ) : null}

        {banner !== null ? (
          <p className="calib__lease-banner" role="alert">
            {banner}
          </p>
        ) : null}
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
        {reportedProfileMismatch ? (
          <p className="facet__note" role="status">
            {`The Player reported ${observation.width_px} × ${observation.height_px} at its last start; this observation may be stale. The Frame profile is ${profile.width_px} × ${profile.height_px}. ${frame.calibration_valid ? `Committed rotation ${rotation}° was considered.` : "Calibration is not yet valid for this binding."} Verify the display and intended rotation, and restart the Player if the display changed. If the profile is wrong, unbind this Frame, edit its profile, then bind and calibrate it.`}
          </p>
        ) : null}
        {profileDraft == null ? (
          <>
            <button ref={profileEditButtonRef} type="button" onClick={beginProfileEdit}>Edit profile</button>
            {profileStatus ? <p className="facet__draft-status" role="status">{profileStatus}</p> : null}
          </>
        ) : (
          <form className="facet__profile-form" onSubmit={saveProfile} aria-label="Edit display profile">
            <p className="facet__note">
              Changing the persistent display profile requires this Frame to be unbound and any active Run to finish or be cancelled. The change clears calibration; recalibrate before showing content.
            </p>
            <label>Pixel width
              <input autoFocus type="number" min="1" max="16384" step="1" value={profileDraft.width_px}
                onChange={(event) => setProfileDraft({ ...profileDraft, width_px: event.target.value })} />
            </label>
            <label>Pixel height
              <input type="number" min="1" max="16384" step="1" value={profileDraft.height_px}
                onChange={(event) => setProfileDraft({ ...profileDraft, height_px: event.target.value })} />
            </label>
            <label>Diagonal (inches)
              <input type="number" min="0" step="any" value={profileDraft.diagonal_inches}
                onChange={(event) => setProfileDraft({ ...profileDraft, diagonal_inches: event.target.value })} />
            </label>
            <label><input type="checkbox" checked={profileDraft.video}
              onChange={(event) => setProfileDraft({ ...profileDraft, video: event.target.checked })} /> Video capable</label>
            {profileError ? <p role="alert">{profileError}</p> : null}
            <button type="submit" disabled={profileSaving}>{profileSaving ? "Saving…" : "Save profile"}</button>
            <button type="button" disabled={profileSaving} onClick={() => {
              setProfileDraft(null);
              setProfileError(null);
              requestAnimationFrame(() => profileEditButtonRef.current?.focus());
            }}>Cancel</button>
          </form>
        )}
      </section>

      <section className="facet__section" role="group" aria-label="Display at last Player start">
        <h4 className="facet__subtitle">Display at last Player start</h4>
        <p className="facet__note">
          Reported by the Player when it started; a display change after that is not seen.
        </p>
        {observation ? (
          <dl className="facet__fields">
            <div className="facet__field">
              <dt>Display</dt>
              <dd>{observation.connected ? "Detected" : "Not detected"}</dd>
            </div>
            <div className="facet__field">
              <dt>Output resolution</dt>
              <dd>{`${observation.width_px} × ${observation.height_px}`}</dd>
            </div>
          </dl>
        ) : (
          <p className="facet__empty">
            {bound
              ? "No report from the bound output."
              : "No Display bound — bind a Player output first."}
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
