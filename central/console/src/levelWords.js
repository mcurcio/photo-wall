/*
 * A display's own level in the console's words (decision 0022; owner, 2026-10-12, picked "Percent,
 * raw count second"): the percent of the display's range first, its own count second, "46 % (117
 * of 255)". The percent is contracts/node_picture.py `HardwareRange.percent`, the base page's
 * rule: a whole percent rounded half up (tests/test_console_level_words.py holds the two to each
 * other). Pure, so Node runs it in that test.
 */

/**
 * `value` of `maximum` as a whole percent, rounded half up (117 of 255 is 46).
 *
 * @param {number} value 0..maximum
 * @param {number} maximum >= 1
 * @returns {number}
 */
export function levelPercent(value, maximum) {
  return Math.floor((value * 200 + maximum) / (2 * maximum));
}

/**
 * "46 % (117 of 255)".
 *
 * @param {number} value 0..maximum
 * @param {number} maximum >= 1
 * @returns {string}
 */
export function levelWords(value, maximum) {
  return `${levelPercent(value, maximum)} % (${value} of ${maximum})`;
}
