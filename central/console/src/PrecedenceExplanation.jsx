import React from "react";

/**
 * Central's Runs on one frame, as join.js `explainPrecedence` states them: the heading, one
 * row per layer (winner first), and the limit line, always shown. Shared by the Wall's
 * Status facet (StatusFacet.jsx) and the Show side's Runs "Why" panel (RunsRegion.jsx), so
 * the Show side imports nothing named for a Wall facet (console DDD §34; R4).
 *
 * @param {{explanation: ReturnType<typeof import("./join.js").explainPrecedence>,
 *          listLabel: string, listClass: string, emptyClass: string}} props
 */
export function PrecedenceExplanation({ explanation, listLabel, listClass, emptyClass }) {
  if (explanation === null) {
    return <p className={emptyClass}>No contributions target this frame.</p>;
  }
  return (
    <div className="precedence">
      <p className="precedence__heading">{explanation.heading}</p>
      <ol className={listClass} aria-label={listLabel}>
        {explanation.rows.map(({ intent, sentence }) => (
          <li key={`${intent.run_id}:${intent.target}`}>{sentence}</li>
        ))}
      </ol>
      <p className="precedence__limit">{explanation.limit}</p>
    </div>
  );
}
