/**
 * Clock times in words (console DDD §35, §41; design language §7, Units, times and numbers): the
 * ONE module that turns an instant or a house time into display words, on the house's clock.
 * tests/test_console_planned.py scans the console and fails when any other module calls a
 * display formatter (`toLocaleTimeString`, `toLocaleString`, `toLocaleDateString`,
 * `Intl.DateTimeFormat`).
 *
 * The house's clock is Settings › House (#2 its zone, #5 its 12- or 24-hour clock), set once read
 * (houseClock.js `useHouseClock`, the one caller of `setHouseClock`): every instant is shown in the
 * house's zone and every time in its clock, so the console shows one clock. Until the House is read,
 * times are in this browser's zone with its abbreviation ("18:00 BST"), on a 24-hour clock.
 *
 * Every function reads only its argument, the house's clock set here and the browser's locale.
 */

const pad = (value) => String(value).padStart(2, "0");

const toDate = (epochSeconds) => new Date(epochSeconds * 1000);

/** The house's zone (null until Settings › House is read) and its clock. */
let house = { zone: null, clock: "24h" };

/**
 * The house's clock from Central's Settings › House: its zone and "12h" or "24h". Only
 * houseClock.js calls it.
 *
 * @param {{zone: string|null, clock: "12h"|"24h"}} settings
 */
export function setHouseClock({ zone, clock }) {
  house = { zone: zone || null, clock: clock === "12h" ? "12h" : "24h" };
}

/** This browser's own time zone, "Europe/London": only offered on Settings › House. */
export function browserZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** The zone every time is shown in: the house's, or this browser's before the House is read. */
export function zoneName() {
  return house.zone ?? browserZone();
}

/**
 * The one zone line, on every page that shows times: "Times on the house clock (Europe/London)";
 * `zone`, a house zone a page read itself (the week page's).
 *
 * @param {string|null} [zone]
 */
export function zoneNote(zone = house.zone) {
  return zone === null ? `Times in ${zoneName()} (this browser's time zone)` : `Times on the house clock (${zone})`;
}

/**
 * A time of day on the house's clock: "18:05" or "6:05 pm"; with `seconds`, "18:05:07" or
 * "6:05:07 pm". The one place the 12- or 24-hour clock is applied.
 *
 * @param {number} hours 0-23
 * @param {number} minutes
 * @param {number|null} [seconds]
 * @returns {string}
 */
export function clockWords(hours, minutes, seconds = null) {
  const tail = seconds === null ? "" : `:${pad(seconds)}`;
  if (house.clock === "24h") return `${pad(hours)}:${pad(minutes)}${tail}`;
  return `${hours % 12 === 0 ? 12 : hours % 12}:${pad(minutes)}${tail} ${hours < 12 ? "am" : "pm"}`;
}

/** An hour mark on a day's axis: "06:00" or "6 am". */
export function hourWords(hour) {
  const hours = hour % 24;
  if (house.clock === "24h") return `${pad(hours)}:00`;
  return `${hours % 12 === 0 ? 12 : hours % 12} ${hours < 12 ? "am" : "pm"}`;
}

/** A house time string's clock part ("2026-10-12T17:05" or "17:05") on the house's clock. */
export function localClockWords(local) {
  if (!/\d\d:\d\d$/.test(local)) return local;
  const [hours, minutes] = local.slice(-5).split(":").map(Number);
  return clockWords(hours, minutes);
}

const formatters = new Map();

/** The parts of `date` in the shown zone, on a 24-hour clock. */
function zonedParts(date) {
  const key = zoneName();
  if (!formatters.has(key)) {
    formatters.set(key, new Intl.DateTimeFormat("en-GB", {
      timeZone: key, year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
      second: "2-digit", hourCycle: "h23",
    }));
  }
  const parts = Object.fromEntries(formatters.get(key).formatToParts(date).map((part) => [part.type, part.value]));
  return {
    year: Number(parts.year), month: Number(parts.month), day: Number(parts.day),
    hours: Number(parts.hour), minutes: Number(parts.minute), seconds: Number(parts.second),
  };
}

function zonePart(date, timeZoneName) {
  return new Intl.DateTimeFormat(undefined, { timeZoneName, timeZone: zoneName() })
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
  return short === null || /^(GMT|UTC)[+\-−]/.test(short) ? offsetLabel(date) : short;
}

/** "18:00" or "6:00 pm", with seconds only when they are not zero; no zone. */
function hourMinute(date) {
  const { hours, minutes, seconds } = zonedParts(date);
  return clockWords(hours, minutes, seconds === 0 ? null : seconds);
}

/**
 * One of Central's times as a clock time on the house's clock: "18:00", with seconds only when
 * they are not zero ("18:00:05"). Before the House is read, in this browser's zone with its
 * abbreviation ("18:00 BST").
 *
 * @param {number} epochSeconds
 * @returns {string}
 */
export function clockTime(epochSeconds) {
  const date = toDate(epochSeconds);
  return house.zone === null ? `${hourMinute(date)} ${zoneLabel(date)}` : hourMinute(date);
}

/**
 * A time of day with its seconds and no zone, "21:04:07": when the Pi acknowledged a live
 * change (Central's time of it), or when this browser saved or left one.
 *
 * @param {number} epochSeconds
 * @returns {string}
 */
export function timeOfDay(epochSeconds) {
  const { hours, minutes, seconds } = zonedParts(toDate(epochSeconds));
  return clockWords(hours, minutes, seconds);
}

/**
 * When a check or a contact last happened, or since when something needs a look (design
 * language §7, "Since"): the weekday and a time, "Sun 03:39". With `nowSeconds` (a
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
  const { hours, minutes } = zonedParts(date);
  const options = Number.isFinite(nowSeconds) && nowSeconds - epochSeconds > 6 * 86400
    ? { day: "numeric", month: "short" }
    : { weekday: "short" };
  return `${date.toLocaleDateString("en-GB", { ...options, timeZone: zoneName() })} ${clockWords(hours, minutes)}`;
}

/** "Tue 2 Mar". */
export function dayLabel(epochSeconds) {
  return toDate(epochSeconds).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: zoneName(),
  });
}

/** A capture date, "12 Dec 2024": a day, not a clock time, so it carries no zone. */
export function captureDay(epochSeconds) {
  return toDate(epochSeconds).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: zoneName(),
  });
}

/**
 * A day and a clock time: "Tue 2 Mar 18:00"; with `year`, "Tue 2 Mar 2027 18:00".
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
      timeZone: zoneName(),
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

/** A start or end of a When: "17:00" (or "5:00 pm"), "sunset", "sunrise +15 min", "sunset −30 min". */
export function timeOfDayWords(time) {
  if (time.at === "clock") return localClockWords(time.time);
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

/** "2026-10-12T17:00" (house clock) as its day, "Mon 12 Oct", and its time, "17:00" or "5:00 pm". */
export function houseParts(local) {
  const [date, time] = local.split("T");
  const [year, month, day] = date.split("-").map(Number);
  const weekday = DAY_KEYS[(new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7];
  return { day: `${DAY_NAMES[weekday]} ${day} ${MONTHS[month - 1]}`, time: localClockWords(time) };
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

// --- The house calendar (Power and the week, the week page): dates and times on the house clock
// are Central's strings ("2026-10-12", "2026-10-12T23:00"). Calendar arithmetic on a date is
// zone-free (UTC dates); the one instant this module turns into house time is a served read time
// (`houseLocal`), in the house's zone Central named, never the browser's.

/** The house time of Central's instant `epochSeconds` in Central's zone `zone`: "2026-10-12T17:05". */
export function houseLocal(epochSeconds, zone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(toDate(epochSeconds)).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

const dateOf = (text) => {
  const [year, month, day] = text.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
};
const dateText = (date) => date.toISOString().slice(0, 10);

/** House date `date` ("2026-10-12") moved by `days` calendar days. */
export function addDays(date, days) {
  const moved = dateOf(date);
  moved.setUTCDate(moved.getUTCDate() + days);
  return dateText(moved);
}

/** The Monday on or before house date `date`: a week page's first day. */
export function mondayOf(date) {
  return addDays(date, -((dateOf(date).getUTCDay() + 6) % 7));
}

/** A house date as its day words: "Mon 12 Oct". */
export function houseDayWords(date) {
  return houseParts(`${date}T00:00`).day;
}

/**
 * When a house time is due, from the house time `now` (both Central's strings): its time alone,
 * "07:00", within the next day; else with its day, "Tue 13 Oct 07:00".
 *
 * @param {string} local
 * @param {string|null} now
 * @returns {string}
 */
export function untilWords(local, now) {
  const { day, time } = houseParts(local);
  if (now !== null && local.slice(0, 10) <= addDays(now.slice(0, 10), 1) && local > now) {
    return local.slice(0, 10) === now.slice(0, 10) || local.slice(11) <= now.slice(11) ? time : `${day} ${time}`;
  }
  return `${day} ${time}`;
}

/**
 * When a house time falls, from the house time `now` (both Central's strings): "18:12 today",
 * "07:00 tomorrow", else "Tue 13 Oct 07:00". The resolved time beside a sunrise or sunset rule
 * (design language §8 E3), from Central's next time, never worked out here.
 *
 * @param {string} local
 * @param {string|null} now
 * @returns {string}
 */
export function dayOfWords(local, now) {
  const { day, time } = houseParts(local);
  if (now !== null && local.slice(0, 10) === now.slice(0, 10)) return `${time} today`;
  if (now !== null && local.slice(0, 10) === addDays(now.slice(0, 10), 1)) return `${time} tomorrow`;
  return `${day} ${time}`;
}
