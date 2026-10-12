/**
 * Clock times in words (console DDD §35, §41): the ONE module that turns an instant into
 * display words. Every clock time it returns carries the browser's zone abbreviation, so two
 * operators in different zones never read one time as if it were the other's.
 * tests/test_console_planned.py scans the console and fails when any other module calls a
 * display formatter (`toLocaleTimeString`, `toLocaleString`, `toLocaleDateString`,
 * `Intl.DateTimeFormat`). Field arithmetic (`getHours` and friends) that builds
 * `datetime-local` input values is not display and stays where it is.
 *
 * Pure: every function reads only its argument and the browser's zone and locale.
 */

const pad = (value) => String(value).padStart(2, "0");

const toDate = (epochSeconds) => new Date(epochSeconds * 1000);

/** The browser's time zone, "Europe/London": every entered and shown time uses it. */
export function zoneName() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** "Times in Europe/London (this browser's time zone)": the zone line on Schedule and Now. */
export function zoneNote() {
  return `Times in ${zoneName()} (this browser's time zone)`;
}

function zonePart(date, timeZoneName) {
  return new Intl.DateTimeFormat(undefined, { timeZoneName })
    .formatToParts(date)
    .find((part) => part.type === "timeZoneName")?.value ?? null;
}

/**
 * The zone's UTC offset at `date`, "UTC+01:00" ("UTC" at offset zero).
 *
 * @param {Date} date
 * @returns {string}
 */
export function offsetLabel(date) {
  const offset = zonePart(date, "longOffset");
  return offset === null ? "local time" : offset.replace(/^GMT/, "UTC");
}

/**
 * The zone's abbreviation at `date`: "BST", "PST", "UTC"; the UTC offset ("UTC+01:00") when
 * the locale has no abbreviation for it and would print a bare "GMT+1".
 *
 * @param {Date} date
 * @returns {string}
 */
export function zoneLabel(date) {
  const short = zonePart(date, "short");
  return short === null || /^(GMT|UTC)[+\-\u2212]/.test(short) ? offsetLabel(date) : short;
}

/** "18:00", with seconds only when they are not zero; no zone. */
function hourMinute(date) {
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.getSeconds() === 0 ? time : `${time}:${pad(date.getSeconds())}`;
}

/**
 * One of Central's times as a clock time in the browser's zone: "18:00 BST", with seconds
 * only when they are not zero ("18:00:05 BST").
 *
 * @param {number} epochSeconds
 * @returns {string}
 */
export function clockTime(epochSeconds) {
  const date = toDate(epochSeconds);
  return `${hourMinute(date)} ${zoneLabel(date)}`;
}

/**
 * A time of day with its seconds and no zone, "21:04:07": when the Pi acknowledged a live
 * change (Central's time of it), or when this browser saved or left one.
 *
 * @param {number} epochSeconds
 * @returns {string}
 */
export function timeOfDay(epochSeconds) {
  const date = toDate(epochSeconds);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * When a check or a contact last happened, or since when something needs a look (design
 * language §7, "Since"): the weekday and a 24-hour time, "Sun 03:39". With `nowSeconds` (a
 * served read time on the same clock, never this browser's: R10), a day more than six days
 * before it names its date instead, "12 Sep 03:39". No zone abbreviation and no seconds: seconds
 * are for acknowledgements ({@link timeOfDay}). The one format of every check and contact time
 * on the photo pages (Photo source, Sources, Scene Photos, Show now, Photo library, Needs a look).
 *
 * @param {number} epochSeconds
 * @param {number} [nowSeconds] the served read time the six days count back from
 * @returns {string}
 */
export function sinceTime(epochSeconds, nowSeconds) {
  const date = toDate(epochSeconds);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const options = Number.isFinite(nowSeconds) && nowSeconds - epochSeconds > 6 * 86400
    ? { day: "numeric", month: "short" }
    : { weekday: "short" };
  return `${date.toLocaleDateString("en-GB", options)} ${time}`;
}

/** "Tue 2 Mar". */
export function dayLabel(epochSeconds) {
  return toDate(epochSeconds).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

/**
 * The label beside capture days (§37): a day depends on the zone it is read in, and these
 * are read in the browser's (an Installation time zone is deferred, PR 37 Q5).
 */
export const DATES_NOTE = "Dates in this browser's time zone";

/** A capture date, "12 Dec 2024": a day, not a clock time, so it carries no zone. */
export function captureDay(epochSeconds) {
  return toDate(epochSeconds).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/**
 * A day and a clock time: "Tue 2 Mar 18:00 BST"; with `year`, "Tue 2 Mar 2027 18:00 BST".
 *
 * @param {number} epochSeconds
 * @param {{year?: boolean}} [options]
 * @returns {string}
 */
export function dateTime(epochSeconds, { year = false } = {}) {
  const day = year
    ? toDate(epochSeconds).toLocaleDateString(undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
    })
    : dayLabel(epochSeconds);
  return `${day} ${clockTime(epochSeconds)}`;
}

// --- House-clock words (Power and the week): a Program's When and its windows are on the house
// clock, worked out in Central; these words only spell them out and never read the browser's zone.

const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Days in words: "Every day", "Mon–Fri" (a run of three or more), "Mon, Wed, Fri". */
function dayWords(days) {
  const ticked = DAY_KEYS.filter((day) => days.includes(day));
  if (ticked.length === DAY_KEYS.length) return "Every day";
  if (ticked.length === 0) return "No day";
  const runs = [];
  ticked.forEach((day) => {
    const last = runs.at(-1);
    if (last !== undefined && DAY_KEYS.indexOf(last.at(-1)) === DAY_KEYS.indexOf(day) - 1) last.push(day);
    else runs.push([day]);
  });
  return runs.map((run) => (run.length >= 3 ? `${DAY_NAMES[run[0]]}–${DAY_NAMES[run.at(-1)]}`
    : run.map((day) => DAY_NAMES[day]).join(", "))).join(", ");
}

/** A start or end of a When: "17:00", "sunset", "sunrise +15 min", "sunset −30 min". */
function timeOfDayWords(time) {
  if (time.at === "clock") return time.time;
  const offset = time.offset_minutes ?? 0;
  if (offset === 0) return time.at;
  return `${time.at} ${offset > 0 ? "+" : "−"}${Math.abs(offset)} min`;
}

/**
 * A Program's When: "Mon–Fri, 17:00–23:00", with " (ends next day)" when a clock end is at or
 * before its start (#76: crossing midnight is shown, never asked).
 *
 * @param {{days: string[], start: object, end: object}} when
 * @returns {string}
 */
export function whenWords(when) {
  const crosses = when.start.at === "clock" && when.end.at === "clock" && when.end.time <= when.start.time;
  return `${dayWords(when.days)}, ${timeOfDayWords(when.start)}–${timeOfDayWords(when.end)}` +
    (crosses ? " (ends next day)" : "");
}

/** "2026-10-12T17:00" (house clock) as its day, "Mon 12 Oct", and its time, "17:00". */
function houseParts(local) {
  const [date, time] = local.split("T");
  const [year, month, day] = date.split("-").map(Number);
  const weekday = DAY_KEYS[(new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7];
  return { day: `${DAY_NAMES[weekday]} ${day} ${MONTHS[month - 1]}`, time };
}

/**
 * One Program window on the house clock, from Central's ProgramWindowView: "Mon 12 Oct
 * 17:00–23:00"; across days, "Mon 12 Oct 22:00 – Tue 13 Oct 02:00".
 *
 * @param {{start: string, end: string}} window
 * @returns {string}
 */
export function windowWords(window) {
  const start = houseParts(window.start);
  const end = houseParts(window.end);
  return start.day === end.day ? `${start.day} ${start.time}–${end.time}`
    : `${start.day} ${start.time} – ${end.day} ${end.time}`;
}

/**
 * A count in the house's number words, with its noun: "41,203 photos", "1 person" (design
 * language §7, Counts: locale grouping, exact). The one home of count words, beside the times.
 *
 * @param {number} count
 * @param {string} noun the singular
 * @param {string} [plural]
 * @returns {string}
 */
export function countWords(count, noun, plural = `${noun}s`) {
  return `${countText(count)} ${count === 1 ? noun : plural}`;
}

/** A bare count with locale grouping: "41,203". */
export function countText(count) {
  return new Intl.NumberFormat().format(count);
}

/**
 * A size in pixels, each side grouped like every count: "1,920 × 1,080" (design review round
 * 2: one number format everywhere). The one home of how a pixel size reads.
 *
 * @param {number} width
 * @param {number} height
 * @returns {string}
 */
export function pixelSize(width, height) {
  return `${countText(width)} × ${countText(height)}`;
}
