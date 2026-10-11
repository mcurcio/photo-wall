/** A Frame's health as health.js classifies it (its severity, its words with and without age). */
export interface FrameHealth { state: string; severity: "ok" | "todo" | "alarm"; label: string; tileLabel: string }

/** How a Frame's health is drawn on a chip: the severity and the words. */
export interface FrameChip { severity: "ok" | "todo" | "notice" | "alarm"; label: string }

/**
 * A Frame's health as a chip, the one way every surface draws it (the Frame page header, the
 * Frame picker, a Scene's Frame chips, the Now page's badges): unfinished setup (a Pi to choose,
 * its Position again) stays `todo` (design language §6), drawn in its own colour, never the
 * accent's; a Pi enrolled that has not reported yet is "Getting ready" (a status word, `notice`),
 * not its enrollment age. `short`: the plan tile's words, without the age.
 */
export function frameChip(health: FrameHealth, short = false): FrameChip {
  if (health.state === "awaiting-report" && health.severity === "todo") return { severity: "notice", label: "Getting ready" };
  return { severity: health.severity, label: short ? health.tileLabel : health.label };
}
