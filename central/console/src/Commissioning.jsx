import React from "react";

import { boundOutput, connectivity } from "./join.js";
import { derive } from "./capability.js";
import { GatedArea } from "./GatedArea.jsx";

/**
 * Read-only Commissioning facet (Bead 4) — design §7.1/§7.2/§7.6.
 *
 * The Display↔Frame hardware relationship, READ-ONLY at T0 (editing is Bead 7).
 * It shows four honest things and gates the rest off:
 *
 *  1. **Committed calibration** — the committed geometry (`corners`/`crop`/
 *     `rotation`) and the SDR `gain`, read from `FrameInventory.calibration`.
 *     `gain` is labelled **"SDR gain"** (design §7.3), never "brightness/color".
 *  2. **Frame facts** — `FrameProfile {width_px, height_px, diagonal_inches,
 *     video}`. These are OPERATOR-DECLARED at create_frame and persist across a
 *     panel swap, so they are labelled **Frame facts**, NOT live Display readback
 *     (design §7.2 provenance rule; the F5 review finding).
 *  3. **Live Display readback** — `OutputReport {width_px, height_px, connected}`,
 *     resolved through the frame's binding to its OutputInventory row on the
 *     COMPOUND key (player_id, output_id) via `boundOutput` (the one compound-key
 *     join rule shared with connectivity(), join.js). This is the ONLY live
 *     Display readback that exists; nothing richer (EDID/model/refresh/HDR) is
 *     invented.
 *  4. **The bound Player/Output** — the equipment path.
 *
 * The panel-color and display-power/params areas are HARDWARE controls with no
 * wired path at T0, so each is a {@link GatedArea} fed a `derive(...)` state that
 * is always "absent" today — rendering an explicit "not yet available" and no
 * enabled control (default-closed capability gate, design §7.6).
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function Commissioning({ snapshot, frameId }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);

  if (!frame) {
    return (
      <div className="facet facet--commissioning">
        <h3 className="facet__title">Commissioning</h3>
        <p className="facet__empty">This frame is no longer in the inventory.</p>
      </div>
    );
  }

  const calibration = frame.calibration ?? {};
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
