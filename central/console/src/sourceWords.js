import { captureDay, clockTime, countWords, dateTime } from "./timeWords.js";

/**
 * A Photo source in words (roadmap 4): one home for what its query includes and for its check's
 * state, read by the Sources list, the Source page, the Scene editor, Show now and the media
 * pipeline (through mediaHealth.js `sourceState`). Pure: it imports only `timeWords.js`.
 *
 * The check states are central/photo_library/model.py `CheckState`'s. Per C7 a failed check never
 * empties anything: the last pool keeps playing, so a failure is `notice` with the words "Photos
 * keep playing", never an alarm.
 */

/** "1 photo", "41,203 photos" (timeWords.js `countWords`, the one home of count words). */
export const counted = countWords;

/**
 * A check time against the media read time (both the database's clock): "21:04 BST" today,
 * else with its day.
 *
 * @param {number} at
 * @param {number|null|undefined} now `media.read_at`
 */
export function checkTime(at, now) {
  return Number.isFinite(now) && captureDay(at) === captureDay(now) ? clockTime(at) : dateTime(at);
}

export const PHOTOS_KEEP_PLAYING = "Photos keep playing.";
export const FAILED_WORDS = {
  connection: "Immich could not be read: see Photo library",
  chosen_item_gone: "An album, person or tag in this Source was deleted in Immich",
};
export const INCOMPLETE_WORDS = "Checked part of it: more than 1,000 photos share one moment";

/**
 * One check's state: `{state, severity, label}`; `label` is one line.
 *
 * @param {object|null|undefined} check a served `CheckState`
 * @param {number|null|undefined} now `media.read_at`, or a pool view's `read_at`
 * @returns {{state: string, severity: "ok"|"todo"|"notice"|"unknown", label: string}}
 */
export function checkWords(check, now) {
  switch (check?.state) {
    case "never_checked":
      return { state: "never-checked", severity: "todo", label: "Not checked yet" };
    case "checking": {
      const last = check.last_complete_at == null ? "" : ` · last complete check ${checkTime(check.last_complete_at, now)}`;
      return { state: "checking", severity: "notice", label: `Checking: ${counted(check.seen_so_far, "photo")} so far${last}` };
    }
    case "complete":
      return { state: "complete", severity: "ok", label: `Checked ${checkTime(check.at, now)}` };
    case "incomplete":
      return { state: "incomplete", severity: "notice", label: INCOMPLETE_WORDS };
    case "failed":
      return {
        state: "failed",
        severity: "notice",
        label: `${FAILED_WORDS[check.problem] ?? "The last check failed"}. ${PHOTOS_KEEP_PLAYING}`,
      };
    default:
      return { state: "unknown", severity: "unknown", label: "Its check was not served" };
  }
}

/** The last complete check, when the check served one: "Last complete check 21:04". */
export function lastCompleteWords(check, now) {
  const at = check?.state === "complete" ? check.at : check?.last_complete_at ?? null;
  return at == null ? null : `Last complete check ${checkTime(at, now)}`;
}

const MEDIA_WORDS = { photo: "photos", live_photo: "live photos", video: "videos" };

/** "last 3 years", "between 1 Jan 2020 and 31 Dec 2022", or null for any time. */
export function takenWords(taken) {
  if (taken == null) return null;
  if (taken.kind === "last") {
    const parts = [taken.years > 0 ? counted(taken.years, "year") : null,
      taken.months > 0 ? counted(taken.months, "month") : null].filter(Boolean);
    return `last ${parts.join(" and ")}`.replace(/^last 1 (year|month)$/, "last $1");
  }
  if (taken.start != null && taken.end != null) return `between ${captureDay(taken.start)} and ${captureDay(taken.end - 1)}`;
  if (taken.start != null) return `from ${captureDay(taken.start)}`;
  if (taken.end != null) return `until ${captureDay(taken.end - 1)}`;
  return null;
}

/**
 * What a query includes, in one line: "Anna and Ben · last 3 years · no screenshots". Chosen
 * albums, people and tags are named when `names` knows them, else counted.
 *
 * @param {object|null} query a served `SourceQuery`
 * @param {Readonly<Record<string, string>>} [names] album, person or tag ref -> its name
 * @returns {string}
 */
export function queryWords(query, names = {}) {
  if (query == null) return "Made before Photo Wall read Immich directly";
  const chosen = (refs, noun, plural) => {
    if (refs.length === 0) return null;
    return refs.every((ref) => Object.hasOwn(names, ref))
      ? refs.map((ref) => names[ref]).join(" and ")
      : counted(refs.length, noun, plural);
  };
  const media = query.media_types ?? [];
  const parts = [
    chosen(query.people ?? [], "person", "people"),
    chosen(query.albums ?? [], "album"),
    query.place ? [query.place.city, query.place.state, query.place.country].filter(Boolean).join(", ") : null,
    takenWords(query.taken),
    query.favourites_only ? "favourites only" : null,
    chosen(query.tags ?? [], "tag"),
    media.length > 0 && media.length < 3 ? `${media.map((type) => MEDIA_WORDS[type]).join(" and ")} only` : null,
    query.partners ? "with partners' photos" : null,
  ].filter(Boolean);
  return [...(parts.length === 0 ? ["Every photo"] : parts), ...excludedWords(query.exclude ?? {}, names)].join(" · ");
}

function excludedWords(exclude, names) {
  const left = [...(exclude.people ?? []), ...(exclude.albums ?? []), ...(exclude.tags ?? [])];
  const words = left.length === 0 ? [] : [`without ${left.every((ref) => Object.hasOwn(names, ref))
    ? left.map((ref) => names[ref]).join(" or ") : counted(left.length, "choice")}`];
  return exclude.screenshots === false ? words : [...words, "no screenshots"];
}
