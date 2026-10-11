/** A Frame's health as health.js classifies it (its severity, its words with and without age). */
export interface FrameHealth { state: string; severity: "ok" | "todo" | "alarm"; label: string; tileLabel: string }

/** How a Frame's health is drawn on a chip: the severity and the words. */
export interface FrameChip { severity: "ok" | "notice" | "alarm"; label: string }

/**
 * A Frame's health as a chip (design review round 2, finding 5): a Frame that still needs
 * something (a Pi, a first report, its Position again) is a `notice`, never the `todo` blue the
 * accent reads as; a Pi enrolled that has not reported yet is "Getting ready" (design language
 * §6), not its enrollment age. `short`: the plan tile's words, without the age.
 */
export function frameChip(health: FrameHealth, short = false): FrameChip {
  if (health.state === "awaiting-report" && health.severity === "todo") return { severity: "notice", label: "Getting ready" };
  const label = short ? health.tileLabel : health.label;
  return { severity: health.severity === "todo" ? "notice" : health.severity, label };
}
