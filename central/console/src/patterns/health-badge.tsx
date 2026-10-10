import type { Severity } from "../design/tokens";
import { StatusChip } from "../ui/status-chip";

/** What a domain component hands HealthBadge: its model's judgement, already worded. */
export interface Verdict {
  severity: Severity;
  /** The model's words; a pattern never re-judges them (H1). */
  label: string;
  /** "last reported 3 s ago", already worded, or null when there is none. */
  receipt: string | null;
}

/** HealthBadge: one verdict as a severity chip, its receipt beside it. */
export function HealthBadge({ verdict }: { verdict: Verdict }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <StatusChip severity={verdict.severity}>{verdict.label}</StatusChip>
      {verdict.receipt !== null && <span className="text-xs text-muted">{verdict.receipt}</span>}
    </span>
  );
}
