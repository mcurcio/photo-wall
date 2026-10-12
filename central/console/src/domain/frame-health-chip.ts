import { POWER } from "../powerWords.js";

/** A Frame's health as health.js classifies it (its severity, its words with and without age). */
export interface FrameHealth { state: string; severity: "ok" | "todo" | "alarm"; label: string; tileLabel: string }

/** How a Frame's health is drawn on a chip: the severity and the words. */
export interface FrameChip { severity: "ok" | "todo" | "notice" | "alarm"; label: string }

/**
 * A Frame's health as a chip, the one way every surface draws it (the Frame page header, the
 * Frame picker, a Scene's Frame chips, the Now page's badges): unfinished setup (a Pi to choose,
 * its Position again) stays `todo` (design language §6), drawn in its own colour, never the
 * accent's; a Pi enrolled that has not reported yet is "Getting ready" (a status word, `notice`),
 * not its enrollment age. `short`: the plan tile's words, without the age. `resting`: Central's
 * Resting (Why's `power.resting`, central/why/explain.py `is_resting`, its one home): dark on
 * purpose, so a Frame whose health is otherwise fine says **Resting** (design language §6). A
 * failure beats Resting (the amended U1: a failure during intended darkness shows at once): an
 * alarm or a todo keeps its own words, so a dead Pi in Night off never reads as fine.
 */
export function frameChip(health: FrameHealth, short = false, resting = false): FrameChip {
  if (resting && health.severity === "ok") return { severity: "ok", label: POWER.resting };
  if (health.state === "awaiting-report" && health.severity === "todo") return { severity: "notice", label: "Getting ready" };
  return { severity: health.severity, label: short ? health.tileLabel : health.label };
}
