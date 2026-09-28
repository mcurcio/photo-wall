import React from "react";

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
 * The dismissed flag lives in PLANE B — the navigation shell's state, never in
 * the snapshot — so a Plane A refresh (poll, after-mutate, explicit Refresh)
 * replaces the fetched inventory alone and CANNOT resurrect a banner the
 * operator has dismissed (the two-plane rule, design §4a: a refresh merges
 * nothing into Plane B). The shell holds it, not this component, because the
 * Wall page mounts only while it is current (flow design §6): leaving the Wall
 * must not undo a dismissal either.
 *
 * @param {{snapshot: object|null, dismissed: boolean, onDismiss: () => void}} props
 * @returns {JSX.Element|null}
 */
export function Guidance({ snapshot, dismissed, onDismiss }) {
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
        onClick={onDismiss}
      >
        Dismiss guidance
      </button>
    </aside>
  );
}
