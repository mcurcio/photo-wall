import React from "react";

/**
 * Central's Runs on one frame, as join.js `explainPrecedence` states them: the heading, one
 * row per layer (winner first), and the limit line, always shown. Shared by the Frame page's
 * Overview (domain/frame-overview.tsx) and the Show side's Runs "Why" panel (RunsRegion.jsx),
 * so the Show side imports nothing named for the Wall (console DDD §34; R4). The list and
 * empty-line classes default to the Overview's.
 *
 * @param {{explanation: ReturnType<typeof import("./join.js").explainPrecedence>,
 *          listLabel: string, listClass?: string, emptyClass?: string}} props
 */
export function PrecedenceExplanation({
  explanation, listLabel, listClass = "facet__why", emptyClass = "facet__empty",
}) {
  if (explanation === null) {
    return <p className={emptyClass}>No Run puts a layer on this Frame now.</p>;
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
