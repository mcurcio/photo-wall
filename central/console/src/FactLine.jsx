import React from "react";

import { factText } from "./facts.js";

/**
 * The only renderer of facts in the fleet views (console DDD design rule 2): a label and
 * the fact in its kind's one wording (facts.js `factText`). The kind is also carried as a
 * class, so an unknown reads differently from a report at a glance. `suffix`, when given,
 * follows the fact after " · " (the Output interruption's "the Run continues", health.js).
 *
 * @param {{label: string, fact: import("./facts.js").Fact, suffix?: string}} props
 */
export function FactLine({ label, fact, suffix }) {
  return (
    <p className={`fact fact--${fact?.kind ?? "unknown"}`}>
      <span className="fact__label">{`${label}: `}</span>
      {factText(fact)}
      {suffix ? ` · ${suffix}` : null}
    </p>
  );
}
