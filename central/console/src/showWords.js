import WORDS from "./showWords.json" with { type: "json" };

/**
 * The Show's words: the one home of the console's copy for a Show concept the operator names
 * (the Schedule, which Central and the API call a Program; frame.md "Programs (the console's
 * Schedules)"; and a Scene's actions, Show now and Schedule it). Every label, heading, button name
 * and outcome sentence for them is a template in `showWords.json`, filled here; the tests read the
 * same file (tests/browser/console_words.py), so renaming a word is one JSON edit, and
 * tests/test_console_copy_home.py fails a test that types one again. (Power's are powerWords.js.)
 *
 * A template's `{name}`-style holes are filled by `fill`; a missing value leaves the hole as is,
 * so a mistake shows on screen instead of reading "undefined".
 */

/** The Schedule's templates (showWords.json `schedule`). */
export const SCHEDULE = Object.freeze(WORDS.schedule);

/** A Scene card's and a saved Scene's actions (showWords.json `scene`). */
export const SCENE = Object.freeze(WORDS.scene);

/**
 * `template` with each `{key}` replaced by `values[key]`.
 *
 * @param {string} template
 * @param {Record<string, string|number>} [values]
 * @returns {string}
 */
export function fill(template, values = {}) {
  return template.replace(/\{(\w+)\}/g, (hole, key) => (Object.hasOwn(values, key) ? String(values[key]) : hole));
}

/**
 * One Schedule template filled: `schedule("card", {name: "Evenings"})` → "Schedule Evenings".
 *
 * @param {keyof typeof WORDS.schedule} key
 * @param {Record<string, string|number>} [values]
 * @returns {string}
 */
export function schedule(key, values) {
  return fill(SCHEDULE[key], values);
}

/**
 * One Scene template filled: `scene("scheduleCard", {id: "evening"})` → "Schedule Scene evening".
 *
 * @param {keyof typeof WORDS.scene} key
 * @param {Record<string, string|number>} [values]
 * @returns {string}
 */
export function scene(key, values) {
  return fill(SCENE[key], values);
}

/**
 * A stored Schedule by its name, never its id (Central names one made in the console
 * "p-…"): the name from the served Programs, or the id when the Schedule is not served.
 *
 * @param {object|null|undefined} runtime the served runtime (snapshot.runtime)
 * @param {string} programId
 * @returns {string}
 */
export function scheduleName(runtime, programId) {
  return runtime?.programs?.[programId]?.name ?? programId;
}
