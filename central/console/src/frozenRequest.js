/**
 * The two pieces every frozen operator request shares (rule of two): the deep freeze that keeps
 * a request from changing after its dialog built it, and the console's audit reference, dated by
 * Central's read time (never the browser's clock). Pure; each verb's home builds its own body.
 */

/** Deep-freeze a plain value (the frozen request cannot change after it is built). */
export function frozen(value) {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(frozen);
    Object.freeze(value);
  }
  return value;
}

/**
 * The console's audit reference: `console/<Central's date>[/<note>]`.
 *
 * @param {number} readAt Central's read time, in seconds
 * @param {string} [note] a one-word reason, appended when not empty
 * @returns {string}
 */
export const auditRef = (readAt, note = "") =>
  `console/${new Date(readAt * 1000).toISOString().slice(0, 10)}${note ? `/${note}` : ""}`;
