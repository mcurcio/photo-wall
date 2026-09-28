/**
 * Authoring rules (pass 2 slice 3 §5, §6): the one id rule, ids derived from
 * names, and every form's problems. Pure — no React, no fetch.
 *
 * The operator names; the console derives the id. A problem is
 * `{field, message}`: `field` names the form control it belongs to, `message`
 * is the sentence shown beside it and in the submit summary. `immediate`
 * problems show before the field is edited (a name with no usable id).
 *
 * @typedef {{field: string, message: string, immediate?: boolean}} Problem
 */

import { formatAge } from "./health.js";

/**
 * The API identifier rule: contracts/models.py `IDENTIFIER_PATTERN` (Scene,
 * Program and Source ids are path parameters under it). A pytest pins the two
 * equal, so there is one rule.
 */
export const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

// A derived id is cut here, leaving room for a window suffix `-NN` (§7).
const DERIVED_ID_LENGTH = 96;

export const NON_LATIN_REASON =
  "This name needs a Latin letter or digit for its id; type an id.";

const ID_RULE =
  "An id starts with a letter or digit, then letters, digits, -, _, . or :; up to 128.";

/**
 * The id a name saves under: NFKD, marks dropped, lowercased, each run of
 * characters outside `a-z0-9` becomes `-`, trimmed of `-`, cut at 96.
 * "Family Evening" → "family-evening". Null when nothing usable is left
 * (「夕方」): the operator must type an id.
 *
 * @param {string} name
 * @returns {string|null}
 */
export function idFromName(name) {
  const id = name
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, DERIVED_ID_LENGTH)
    .replace(/-+$/, "");
  return id === "" ? null : id;
}

/**
 * The id a draft saves under: the typed id once the operator opened the Id
 * field (`idOverride` not null), otherwise the one derived from its name.
 *
 * @param {{name: string, idOverride: string|null}} draft
 * @returns {string|null}
 */
export function draftId({ name, idOverride }) {
  return idOverride !== null ? idOverride.trim() : idFromName(name);
}

/**
 * The problems every named form shares (§6 "All"): a name, a usable id, and no
 * collision with an existing id of the same kind. The collision sits on the
 * field that decides the id.
 *
 * @param {string} kind "Scene" or "Program"
 * @param {{name: string, idOverride: string|null}} draft
 * @param {Set<string>} existingIds
 * @returns {Problem[]}
 */
export function identityProblems(kind, draft, existingIds) {
  const problems = [];
  const named = draft.name.trim() !== "";
  if (!named) {
    problems.push({ field: "name", message: "Enter a name." });
  }
  const id = draftId(draft);
  if (draft.idOverride === null) {
    if (named && id === null) {
      problems.push({ field: "id", message: NON_LATIN_REASON, immediate: true });
    }
  } else if (id === "" || !IDENTIFIER_PATTERN.test(id)) {
    problems.push({ field: "id", message: ID_RULE });
    return problems;
  }
  if (id !== null && existingIds.has(id)) {
    problems.push({
      field: draft.idOverride !== null ? "id" : "name",
      message: `A ${kind} called ${id} already exists; choose another name.`,
    });
  }
  return problems;
}

/**
 * A Scene draft's problems (§6 Scene), in form order.
 *
 * @param {{name: string, idOverride: string|null, mode: "live"|"authored",
 *          sourceRef: string, targets: string[], cycleSeconds: string|number,
 *          selections: Record<string, string>}} draft
 * @param {Set<string>} existingIds the stored Scene ids
 * @returns {Problem[]}
 */
export function sceneProblems(draft, existingIds) {
  const problems = identityProblems("Scene", draft, existingIds);
  if (draft.sourceRef === "") {
    problems.push({ field: "source", message: "Choose a Source." });
  }
  if (draft.targets.length === 0) {
    problems.push({ field: "targets", message: "Choose at least one frame." });
  }
  if (!(Number(draft.cycleSeconds) > 0)) {
    problems.push({ field: "cycle", message: "Seconds per cycle must be more than 0." });
  }
  if (draft.mode === "authored") {
    for (const frameId of draft.targets) {
      if (!draft.selections[frameId]) {
        problems.push({ field: `media:${frameId}`, message: `Choose media for ${frameId}.` });
      }
    }
  }
  return problems;
}

/** Whether a field value is a whole number (a priority). */
function isWhole(value) {
  return String(value).trim() !== "" && Number.isInteger(Number(value));
}

/**
 * An activation draft's problems (§6, §11).
 *
 * @param {{sceneId: string, priority: string|number}} draft
 * @returns {Problem[]}
 */
export function activationProblems({ sceneId, priority }) {
  const problems = [];
  if (sceneId === "") {
    problems.push({ field: "scene", message: "Choose a Scene." });
  }
  if (!isWhole(priority)) {
    problems.push({ field: "priority", message: "Priority must be a whole number." });
  }
  return problems;
}

/**
 * A fresh activation id, never shown: `console-<base36 ms>-<8 hex>` (§5).
 * Minted when the draft changes or after a definite outcome, and REUSED on a
 * retry after "outcome unknown": Central answers a known id with its stored
 * Admission (central/runtime.py `activate`), so a retry cannot start twice.
 *
 * @returns {string}
 */
export function newActivationKey() {
  const random = crypto.getRandomValues(new Uint32Array(1))[0].toString(16).padStart(8, "0");
  return `console-${Date.now().toString(36)}-${random}`;
}


// --- Programs and the windows helper (§6, §7).

/**
 * A `datetime-local` value (the operator's local time) as POSIX seconds — the
 * unit `Program.starts_at` / `ends_at` carry (central/runtime.py). NaN for an
 * empty or unparseable value.
 *
 * @param {string} local
 * @returns {number}
 */
export function toEpochSeconds(local) {
  return local ? new Date(local).getTime() / 1000 : NaN;
}

/** The browser's time zone, which every entered and shown time uses (§6). */
export function timeZoneName() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** Scene, window and priority problems shared by one Program and the helper. */
function scheduleProblems({ sceneId, start, end, priority }, now) {
  const problems = [];
  if (sceneId === "") {
    problems.push({ field: "scene", message: "Choose a Scene." });
  }
  const startsAt = toEpochSeconds(start);
  const endsAt = toEpochSeconds(end);
  if (!Number.isFinite(startsAt)) {
    problems.push({ field: "start", message: "Enter when the window starts." });
  }
  if (!Number.isFinite(endsAt)) {
    problems.push({ field: "end", message: "Enter when the window ends." });
  } else if (Number.isFinite(startsAt) && endsAt <= startsAt) {
    problems.push({
      field: "end",
      message:
        endsAt === startsAt
          ? "The window ends when it starts."
          : `The window ends ${formatAge(startsAt - endsAt)} before it starts.`,
    });
  } else if (endsAt <= now) {
    problems.push({
      field: "end",
      message: "That window has already ended; Central would record it as missed.",
    });
  }
  if (!isWhole(priority)) {
    problems.push({ field: "priority", message: "Priority must be a whole number." });
  }
  return problems;
}

/**
 * One Program's problems (§6 Program).
 *
 * @param {{name: string, idOverride: string|null, sceneId: string, start: string,
 *          end: string, priority: string|number}} draft
 * @param {Set<string>} existingIds the stored Program ids
 * @param {number} now Central's clock (`current.now`)
 * @returns {Problem[]}
 */
export function programProblems(draft, existingIds, now) {
  return [...identityProblems("Program", draft, existingIds), ...scheduleProblems(draft, now)];
}

export const MAX_WINDOWS = 60;
const MAX_IDENTIFIER = 128;

/**
 * The helper's windows (§7): window 1 is the entered window; each later one
 * falls on the next ticked weekday at the same LOCAL clock times (calendar-day
 * arithmetic, so a DST change keeps 18:00). `weekdays` is indexed like
 * `Date.getDay()` (0 = Sunday).
 *
 * @param {{start: string, end: string, weekdays: boolean[], count: number}} draft
 * @returns {Array<{startsAt: number, endsAt: number}>}
 */
export function planWindows({ start, end, weekdays, count }) {
  const first = new Date(start);
  const last = new Date(end);
  const spanDays = Math.round(
    (Date.UTC(last.getFullYear(), last.getMonth(), last.getDate()) -
      Date.UTC(first.getFullYear(), first.getMonth(), first.getDate())) /
      86400000,
  );
  const at = (day, offset, clock) =>
    new Date(
      day.getFullYear(),
      day.getMonth(),
      day.getDate() + offset,
      clock.getHours(),
      clock.getMinutes(),
    ).getTime() / 1000;
  const windows = [];
  let day = new Date(first.getFullYear(), first.getMonth(), first.getDate());
  for (let n = 0; n < count; n += 1) {
    if (n > 0) {
      if (!weekdays.some(Boolean)) {
        break;
      }
      do {
        day = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1);
      } while (!weekdays[day.getDay()]);
    }
    windows.push({ startsAt: at(day, 0, first), endsAt: at(day, spanDays, last) });
  }
  return windows;
}

/**
 * The helper's problems (§6 Windows): the Program problems without the base
 * id's collision (it is never stored), then the mask, the count, and — once
 * those are sound — each window id's length and collision, and no overlap.
 *
 * @param {{name: string, idOverride: string|null, sceneId: string, start: string,
 *          end: string, priority: string|number, weekdays: boolean[],
 *          count: string|number}} draft
 * @param {Set<string>} existingIds the stored Program ids
 * @param {number} now
 * @returns {Problem[]}
 */
export function windowProblems(draft, existingIds, now) {
  const problems = [...identityProblems("Program", draft, new Set()), ...scheduleProblems(draft, now)];
  if (!draft.weekdays.some(Boolean)) {
    problems.push({ field: "weekdays", message: "Tick at least one weekday." });
  }
  const count = Number(draft.count);
  if (!(Number.isInteger(count) && count >= 1 && count <= MAX_WINDOWS)) {
    problems.push({ field: "count", message: `Between 1 and ${MAX_WINDOWS} windows.` });
  }
  if (problems.length > 0) {
    return problems;
  }
  const id = draftId(draft);
  const idField = draft.idOverride !== null ? "id" : "name";
  const longest = `${id}-${count}`;
  if (longest.length > MAX_IDENTIFIER) {
    problems.push({
      field: idField,
      message: `Name too long: ${longest} must be at most ${MAX_IDENTIFIER} characters.`,
    });
    return problems;
  }
  const windows = planWindows({ ...draft, count });
  windows.forEach((_window, index) => {
    const windowId = `${id}-${index + 1}`;
    if (existingIds.has(windowId)) {
      problems.push({ field: idField, message: `${windowId} already exists.` });
    }
  });
  if (windows.some((window, index) => index > 0 && windows[index - 1].endsAt > window.startsAt)) {
    problems.push({ field: "weekdays", message: "Each window must end before the next starts." });
  }
  return problems;
}

// --- Sources (§6 Source, §7).

/**
 * A `date` input value ("2024-03-01") as the POSIX seconds of that day's local
 * midnight; null when empty.
 *
 * @param {string} value
 * @returns {number|null}
 */
export function localDayStart(value) {
  if (!value) {
    return null;
  }
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day).getTime() / 1000;
}

/**
 * A Source draft's problems (§6 Source).
 *
 * @param {{sourceRef: string, connectionRef: string, capturedFrom: string,
 *          capturedUntil: string}} draft
 * @returns {Problem[]}
 */
export function sourceProblems({ sourceRef, connectionRef, capturedFrom, capturedUntil }) {
  const problems = [];
  if (!IDENTIFIER_PATTERN.test(sourceRef.trim())) {
    problems.push({
      field: "ref",
      message:
        "Name and revision, like holiday:1: a letter or digit, then letters, digits, " +
        "-, _, . or :; up to 128.",
    });
  }
  if (connectionRef.trim() === "") {
    problems.push({ field: "connection", message: "Connection name is required." });
  } else if (!IDENTIFIER_PATTERN.test(connectionRef.trim())) {
    problems.push({ field: "connection", message: ID_RULE });
  }
  const from = localDayStart(capturedFrom);
  const until = localDayStart(capturedUntil);
  if (from !== null && until !== null && until <= from) {
    problems.push({ field: "until", message: "'Taken until' must be after 'Taken from'." });
  }
  return problems;
}
