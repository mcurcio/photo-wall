import React from "react";

import { frameChip, frameStatusIn } from "./domain/frame-status.ts";
import { ReadinessNotice } from "./ReadinessNotice.jsx";

/**
 * Frame status badges on the Now page: one per Frame, its status as Central serves it, drawn
 * through the one chip mapping (domain/frame-status.ts), so a Frame reads in the same
 * colour and words here as on Home and in the Show now step below.
 *
 * A badge is a STATUS, never a control: a Frame that cannot present matters to the
 * showrunner, but every Display CONTROL stays on the Frame page's Position and Picture tabs
 * (R4, J4). This module imports no Wall component.
 *
 * @param {{snapshot: object|null}} props
 */
export function FrameHealthBadges({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  return (
    <section className="showrunner__health" role="group" aria-label="Frame health">
      {frames.map((frame) => {
        const health = frameChip(frameStatusIn(snapshot, frame.id));
        return (
          <div key={frame.id} className="showrunner__badge-item">
            <span
              className={`showrunner__badge health--${health.severity}`}
              aria-label={`Frame ${frame.id}: ${health.label}`}
            >
              {`${frame.id}: ${health.label}`}
            </span>
            <ReadinessNotice snapshot={snapshot} frameId={frame.id} />
          </div>
        );
      })}
    </section>
  );
}
