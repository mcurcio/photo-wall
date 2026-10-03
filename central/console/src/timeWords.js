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

/**
 * A Program window: "Tue 2 Mar 18:00–20:00 BST"; across days or a zone change, each end in
 * full: "Tue 2 Mar 23:00 GMT – Wed 3 Mar 01:00 GMT".
 *
 * @param {{starts_at: number, ends_at: number}} window
 * @returns {string}
 */
export function windowLabel({ starts_at: startsAt, ends_at: endsAt }) {
  const start = toDate(startsAt);
  const end = toDate(endsAt);
  const startDay = dayLabel(startsAt);
  const zone = zoneLabel(start);
  return startDay === dayLabel(endsAt) && zone === zoneLabel(end)
    ? `${startDay} ${hourMinute(start)}–${hourMinute(end)} ${zone}`
    : `${dateTime(startsAt)} – ${dateTime(endsAt)}`;
}

/**
 * A saved repeated occurrence's clock time with its offset, "1:30 AM PST (GMT-08:00)", so a
 * wall-clock time that occurs twice (a daylight-saving change) names which one it is.
 *
 * @param {number} epochSeconds
 * @returns {string}
 */
export function occurrenceTime(epochSeconds) {
  const date = toDate(epochSeconds);
  const clock = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
  return `${clock} (${zonePart(date, "longOffset") ?? "local time"})`;
}
