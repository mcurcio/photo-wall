import React from "react";

/**
 * The Wall's first step (Bead 18; console DDD §54, §61): a non-blocking guidance banner,
 * NOT a modal wizard that gates the console.
 *
 * First-run is inferred from Plane A: an installation with no Frames yet has an
 * empty canvas, so the banner points the installer at the first steps (draw a
 * Frame, power on one Pi, bind the Frame to one of its Outputs, calibrate the
 * Frame — slice 2 §5). It renders only while there are no Frames, and has no
 * dismissal: it leaves by itself when the first Frame exists, and the Wall's To
 * finish list takes over (G2). **Add first frame** calls `onAddFirstFrame`, with
 * which the Wall opens Edit layout (`#/wall/layout`).
 *
 * @param {{snapshot: object|null, onAddFirstFrame: () => void}} props
 * @returns {JSX.Element|null}
 */
export function Guidance({ snapshot, onAddFirstFrame }) {
  const frames = snapshot?.inventory?.frames ?? [];
  if (frames.length > 0) {
    return null;
  }

  return (
    <aside
      className="console__guidance"
      role="note"
      aria-label="Getting started"
    >
      <p className="console__guidance-text">
        Add a frame, power on one Pi, bind the frame to one of its outputs, then
        calibrate the Frame. To show photos, make a Scene targeting that Frame,
        then Show now or Schedule it.
      </p>
      <button type="button" onClick={onAddFirstFrame}>
        Add first frame
      </button>
    </aside>
  );
}
