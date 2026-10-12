import WORDS from "./powerWords.json" with { type: "json" };
import { fill } from "./showWords.js";

/**
 * The Power and week words: the one home of the console's copy for display power (Night off,
 * All off and All on, holds, Resting and the power line). Every label, button name and sentence
 * the tests look for is a template in `powerWords.json`, filled here; the tests read the same file
 * (tests/browser/console_words.py), so a copy change is one JSON edit
 * (tests/test_console_copy_home.py holds every test to it).
 */

/** The power templates (powerWords.json `power`). */
export const POWER = Object.freeze(WORDS.power);

/**
 * One power template filled: `power("band", {what: "Off · Night off", when: "…"})`.
 *
 * @param {keyof typeof WORDS.power} key
 * @param {Record<string, string|number>} [values]
 * @returns {string}
 */
export function power(key, values) {
  return fill(POWER[key], values);
}
