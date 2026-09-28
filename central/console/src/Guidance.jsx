import React, { useState } from "react";

/**
 * Non-blocking, dismissible first-run guidance banner (Bead 18, design Q8:
 * always-visible inventory + a non-blocking guidance banner carries onboarding,
 * NOT a modal wizard that gates the console).
 *
 * First-run is inferred from Plane A: an installation with no Frames yet has an
 * empty canvas, so the banner points the installer at the first steps (draw a
 * Frame, power on one Pi, bind the Frame to one of its Outputs, commission the
 * display — slice 2 §5). Once any Frame exists the banner never shows.
 *
 * The dismissed flag lives in PLANE B — ordinary component-local state, never in
 * the snapshot — so a Plane A refresh (focus/visibility, after-mutate, explicit
 * Refresh) replaces the fetched inventory alone and CANNOT resurrect a banner the
 * operator has dismissed (the two-plane rule, design §4a: a refresh merges
 * nothing into Plane B). The component stays mounted while it returns null, so
 * the dismissed state persists across refreshes.
 *
 * @param {{snapshot: object|null}} props
 * @returns {JSX.Element|null}
 */
export function Guidance({ snapshot }) {
  // Plane B: component-local, seeded false, untouched by any snapshot refresh.
  const [dismissed, setDismissed] = useState(false);

  const frames = snapshot?.inventory?.frames ?? [];
  const firstRun = frames.length === 0;
  if (!firstRun || dismissed) {
    return null;
  }

  return (
    <aside
      className="console__guidance"
      role="note"
      aria-label="Getting started"
    >
      <p className="console__guidance-text">
        Draw a frame, power on one Pi, bind the frame to one of its outputs, then
        commission the display.
      </p>
      <button
        type="button"
        className="console__guidance-dismiss"
        onClick={() => setDismissed(true)}
      >
        Dismiss guidance
      </button>
    </aside>
  );
}
