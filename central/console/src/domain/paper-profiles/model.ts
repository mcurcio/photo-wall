import type { PaperProfileRefusal, PaperProfileView } from "../../generated/contract";
import type { AddAnswer, RemoveAnswer } from "./api";

/*
 * The paper profiles in the console's words: what an add or a remove came to, and who uses one.
 * A paper profile is the colour profile (an ICC file) a paper's maker publishes; Central bakes the
 * LUT a Frame's paper draws with from it. Pure: no React, no request.
 */

/** "MNN", "MNN and Hall", "MNN, Hall and Kitchen". */
export function namesWords(names: readonly string[]): string {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** Each refusal of a paper profile change, in words: every code Central has (a code without words
 * fails the typecheck). */
export const PAPER_PROFILE_REFUSALS: Record<PaperProfileRefusal, string> = {
  not_a_colour_profile: "it is not a colour profile (an ICC file). Download the .icc profile for your paper from its maker.",
  not_a_printer_profile: "it is a colour profile, but not one for a printer and paper. Choose the profile your paper's "
    + "maker publishes for your printer.",
  too_large: "it is too large for a paper profile.",
  unknown_paper_profile: "it is no longer on the list.",
  named_by_a_frame: "a Frame's paper uses it. Choose another paper profile on that Frame's Lighting tab first.",
};
const UNREACHABLE = "Central did not answer. Try again.";

/** A refusal's words: Central's code in words; a code outside the contract (a lost request, an
 * answer outside its shape) in a sentence. */
function refusalWords(code: string): string {
  if (code in PAPER_PROFILE_REFUSALS) return PAPER_PROFILE_REFUSALS[code as PaperProfileRefusal];
  return code === "unreachable" ? UNREACHABLE : `Central refused it (${code}).`;
}

/** What an add came to, in plain words: `file` is the name of the file chosen. */
export function addWords(file: string, answer: AddAnswer): string {
  if (answer.ok) {
    return answer.added
      ? `Added ${answer.profile.name}. Choose it as a Frame's paper profile, on its Lighting tab.`
      : `${answer.profile.name} is already on the list.`;
  }
  return `Not added (${file}): ${refusalWords(answer.error)}`;
}

/** What a remove came to, in plain words, naming the Frames whose paper uses it. */
export function removeWords(profile: PaperProfileView, answer: RemoveAnswer): string {
  if (answer.ok) return `Removed ${profile.name}.`;
  const frames = answer.frames.length === 0 ? "" : ` (${namesWords(answer.frames)})`;
  return `Not removed (${profile.name}): ${refusalWords(answer.error)}${frames}`;
}

/** Who uses a paper profile, in words: the Frames whose paper uses it, or "No Frame". */
export function usedByWords(profile: PaperProfileView): string {
  return profile.used_by.length === 0 ? "No Frame" : namesWords(profile.used_by);
}
