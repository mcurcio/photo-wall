export type AckBadgeProps =
  | { state: "requested" }
  /** `at`: when the Pi acknowledged it, already worded ("21:04:07"); null when not known. `note`: a
   * plain aside in brackets after it ("your last change was refused"). */
  | { state: "acknowledged"; at: string | null; note?: string };

/**
 * AckBadge (design language §4): whether the Pi has acknowledged a requested change, in two
 * states only. A missing acknowledgement past an action's deadline is a ProblemCard, not a
 * third state. It is a polite live region: it says when the acknowledgement arrives.
 */
export function AckBadge(props: AckBadgeProps) {
  const presented = props.state === "requested" ? null
    : props.at === null ? "Presented by the Pi" : `Presented by the Pi · ${props.at}`;
  return (
    <span
      role="status"
      data-state={props.state}
      className="inline-flex max-w-full items-center gap-1.5 rounded-pill border border-line px-2.5 py-0.5 text-xs text-text"
    >
      {presented === null ? "Previewing — waiting for the Pi"
        : props.state === "acknowledged" && props.note !== undefined ? `${presented} (${props.note})` : presented}
    </span>
  );
}
