import React from "react";

import { frameHealth } from "./health.js";
import { ReadinessNotice } from "./ReadinessNotice.jsx";

/**
 * Frame-health badges on the Now page: one per Frame, labelled by the one
 * classifier (health.js) exactly as the Wall labels it.
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
        const health = frameHealth(snapshot, frame.id);
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
