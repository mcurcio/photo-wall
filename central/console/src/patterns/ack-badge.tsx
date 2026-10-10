export type AckBadgeProps =
  | { state: "requested" }
  /** `at`: when the Pi acknowledged it, already worded ("21:04:07"); null when not known. */
  | { state: "acknowledged"; at: string | null };

/**
 * AckBadge (design language §4): whether the Pi has acknowledged a requested change, in two
 * states only. A missing acknowledgement past an action's deadline is a ProblemCard, not a
 * third state. It is a polite live region: it says when the acknowledgement arrives.
 */
export function AckBadge(props: AckBadgeProps) {
  return (
    <span
      role="status"
      data-state={props.state}
      className="inline-flex max-w-full items-center gap-1.5 rounded-pill border border-line px-2.5 py-0.5 text-xs text-text"
    >
      {props.state === "requested" ? "Previewing — waiting for the Pi"
        : props.at === null ? "Presented by the Pi" : `Presented by the Pi · ${props.at}`}
    </span>
  );
}
