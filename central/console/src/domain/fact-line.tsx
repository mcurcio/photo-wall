import type { Severity } from "../design/tokens";
import { type Fact, factText } from "../facts.js";
import { FactRow } from "../patterns/fact-row";

export interface FactLineProps {
  label?: string;
  fact: Fact | null | undefined;
  /** Follows the fact after " · " (the Output interruption's "the Run continues", health.js). */
  suffix?: string;
  /** `false` omits a reported fact's receipt where one line above states it for its record. */
  receipt?: boolean;
  /** The judge's band for this line (hostHealth.js), for the eye only. */
  band?: Severity | null;
}

/**
 * The only renderer of facts (console DDD design rule 2): a label and the fact in its kind's
 * one wording (facts.js `factText`), in its truth kind's tone. With no `label`, the fact
 * stands alone (a `planned` fact names itself: "On top: …", frame-overview.tsx).
 */
export function FactLine({ label, fact, suffix, receipt = true, band = null }: FactLineProps) {
  return (
    <FactRow label={label} tone={fact?.kind ?? "unknown"} band={band}>
      {factText(fact as Fact, { receipt })}
      {suffix ? ` · ${suffix}` : null}
    </FactRow>
  );
}
