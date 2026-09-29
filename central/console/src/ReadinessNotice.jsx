import React from "react";

import { readinessReportForFrame } from "./readinessRecovery.js";

/** A non-interactive explanation of an accepted Player readiness failure. */
export function ReadinessNotice({ snapshot, frameId }) {
  const report = readinessReportForFrame(snapshot, frameId);
  if (report === null) return null;
  return (
    <div className="readiness-notice" role="note" aria-label={`Player readiness for ${frameId}`}>
      <strong>Player readiness report</strong>
      <p className="field__hint">
        These details come from the latest accepted Player report for current assignments;
        they are not a readback of visible pixels.
      </p>
      {report.age !== null && (
        <p className="field__hint">{`Central accepted this report ${report.age} ago.`}</p>
      )}
      <ul className="field__hint">
        {report.messages.map((message) => <li key={message}>{message}</li>)}
      </ul>
    </div>
  );
}
