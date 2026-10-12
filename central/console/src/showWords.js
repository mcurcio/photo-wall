import WORDS from "./showWords.json" with { type: "json" };

/**
 * The Show's words: the one home of the console's copy for a Show concept the operator names
 * (today the Schedule, which Central and the API call a Program; frame.md "Programs (the
 * console's Schedules)"). Every label, heading, button name and outcome sentence that names a
 * Schedule is a template in `showWords.json`, filled here; the browser tests read the same file
 * (tests/browser/console_words.py), so renaming a word is one JSON edit and no test changes.
 *
 * A template's `{name}`-style holes are filled by `fill`; a missing value leaves the hole as is,
 * so a mistake shows on screen instead of reading "undefined".
 */

/** The Schedule's templates (showWords.json `schedule`). */
export const SCHEDULE = Object.freeze(WORDS.schedule);

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
