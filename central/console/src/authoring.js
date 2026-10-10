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
import { frameOf, toTarget } from "./join.js";
import { IDENTIFIER_PATTERN } from "./routes.js";
import { sourceName } from "./sourceNames.js";

// The API identifier rule is routes.js `IDENTIFIER_PATTERN` (re-exported here).
export { IDENTIFIER_PATTERN };

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
 * The labels of a Scene's playback fields (slice 3 §4): the one wording wherever they
 * are asked (CycleInput.jsx, SceneSettings.jsx), answered (the Scene flow's Review) or
 * named (Reload). The last five are delivery 1x's (settings catalogue #41, #62–#65).
 */
export const PLAYBACK_LABELS = Object.freeze({
  cycle: "Seconds per cycle",
  loop: "Keep playing until the Program ends",
  fade: "Fade between photos",
  ending: "How it ends",
  endingSeconds: "Ending length",
  keepLastPhoto: "Keep the last photo up",
  keepTogether: "Keep these Frames together",
});

/**
 * How a Scene ends (settings catalogue #62, #63), as its outro: "none" (no outro), "black"
 * (an opaque black outro covering what plays beneath) or "fade" (one more photo per Frame
 * that fades out, revealing what plays beneath).
 *
 * @typedef {"none"|"black"|"fade"} Ending
 */
export const ENDINGS = Object.freeze(["none", "black", "fade"]);

/**
 * The defaults of delivery 1x's Scene settings (settings catalogue): a 1.5 s fade between
 * photos (#41); no ending (#62, #63: the catalogue's "dissolve 3 s" and "black" are one
 * choice in the built model and disagree, so the built behaviour stays until the owner
 * picks); 3 s once an ending is chosen; the last photo kept up (#64); Frames not held
 * together (#65).
 */
export const SETTING_RANGES = Object.freeze({
  fadeSeconds: Object.freeze({ min: 0, max: 5, step: 0.5 }),
  endingSeconds: Object.freeze({ min: 0.5, max: 10, step: 0.5 }),
});

/** Whether `value` is one the setting's slider can show exactly (in range, on a step). */
export function inRange(value, { min, max, step }) {
  const number = Number(value);
  return number >= min && number <= max && Number.isInteger((number - min) / step);
}

export const SCENE_SETTING_DEFAULTS = Object.freeze({
  fadeSeconds: 1.5,
  ending: "none",
  endingSeconds: 3,
  keepLastPhoto: true,
  keepTogether: false,
});

/**
 * A Scene draft's problems (§6 Scene), in form order. Editing a stored Scene
 * (§13) keeps its stored id, so there is no name, id or collision to check.
 *
 * A hand-picked frame whose candidates were read and are empty (`noMedia`) has
 * nothing to choose: its problem belongs to the frame choice (field `targets`).
 *
 * @param {{name: string, idOverride: string|null, mode: "live"|"authored",
 *          sourceRef: string, targets: string[], cycleSeconds: string|number,
 *          selections: Record<string, string>, loadingMedia?: boolean,
 *          noMedia?: string[]}} draft
 * @param {Set<string>} existingIds the stored Scene ids
 * @param {{editing?: boolean}} [options]
 * @returns {Problem[]}
 */
export function sceneProblems(draft, existingIds, { editing = false } = {}) {
  const problems = editing ? [] : identityProblems("Scene", draft, existingIds);
  if (draft.sourceRef === "") {
    problems.push({ field: "source", message: "Choose a Source." });
  }
  if (draft.targets.length === 0) {
    problems.push({ field: "targets", message: "Choose at least one frame." });
  }
  if (!(Number(draft.cycleSeconds) > 0)) {
    problems.push({ field: "cycle", message: `${PLAYBACK_LABELS.cycle} must be more than 0.` });
  } else if (Number(draft.fadeSeconds) > Number(draft.cycleSeconds)) {
    // A photo's fade out and the next one's fade in share its cycle (central/planner.py
    // refuses fades longer than the cycle).
    problems.push({
      field: "fade",
      message: `${PLAYBACK_LABELS.fade} must be no longer than ${PLAYBACK_LABELS.cycle}.`,
    });
  }
  if (draft.ending === "fade" && Number(draft.endingSeconds) < Number(draft.fadeSeconds) / 2) {
    // "Fades out" brings the last photo back over half the fade, inside the ending.
    problems.push({
      field: "endingSeconds",
      message: `${PLAYBACK_LABELS.endingSeconds} must be at least half of ${PLAYBACK_LABELS.fade}.`,
    });
  }
  if (draft.mode === "authored") {
    for (const frameId of draft.targets) {
      if (draft.selections[frameId]) {
        continue;
      }
      if (draft.loadingMedia) {
        // While a frame's candidates are read, that is the state to say (§6).
        problems.push({ field: `media:${frameId}`, message: "Loading compatible media…" });
      } else if (draft.noMedia?.includes(frameId)) {
        problems.push({
          field: "targets",
          message:
            `No compatible media for ${frameId} in ${sourceName(draft.sourceRef)}. ` +
            "Choose another frame, or another Source.",
        });
      } else {
        problems.push({ field: `media:${frameId}`, message: `Choose media for ${frameId}.` });
      }
    }
  }
  return problems;
}

/**
 * Build the save {path, body} for the given authoring mode (the 14a/14b seam).
 * "live": the plain scene route with the Scene as the body. "authored": the
 * authored route with a {scene, source_ref, asset_ids} body — one media
 * Contribution per target Frame carrying that Frame's chosen asset ref.
 * `revision` is 1 for a new Scene; an edit sends the stored revision + 1 (§13).
 *
 * Delivery 1x's settings (an omitted one writes what the console always wrote):
 * - `fadeSeconds` (#41): each photo fades out over half of it and the next fades in over
 *   the other half (every body Contribution's `fade_out_seconds` and `fade_in_seconds`);
 * - `ending` and `endingSeconds` (#62, #63): the outro. "black" is one opaque black
 *   Contribution per Frame; "fade" is one more media Contribution per Frame, from the same
 *   photos: the last photo returns over half the fade between photos, then fades out over
 *   the rest of the outro (so `endingSeconds` must be at least half the fade);
 * - `keepLastPhoto` (#64): every body Contribution's `retain_on_expiry`;
 * - `keepTogether` (#65): the Scene's `protect_frames`.
 *
 * @param {"live"|"authored"} mode
 * @param {{sceneId: string, sourceRef: string, targetIds: string[], cycleSeconds: number,
 *          loop: boolean, selections?: Record<string,string>, revision?: number,
 *          fadeSeconds?: number, ending?: Ending, endingSeconds?: number,
 *          keepLastPhoto?: boolean, keepTogether?: boolean}} draft
 * @returns {{path: string, body: object}}
 */
export function buildSave(
  mode,
  {
    sceneId, sourceRef, targetIds, cycleSeconds, loop, selections = {}, revision = 1,
    fadeSeconds = 0, ending = "none", endingSeconds = 0, keepLastPhoto = true, keepTogether = false,
  },
) {
  if (mode !== "live" && mode !== "authored") {
    throw new Error(`unsupported scene authoring mode: ${mode}`);
  }
  // What each Frame shows: an authored Scene's chosen asset ref (never a live source_ref),
  // or a live Scene's Source. `target` is the verified string "frame:<id>" (design §1b);
  // role carries the frame id.
  const media = (frameId) => (mode === "authored"
    ? { asset_refs: [selections[frameId]] }
    : { source_refs: [sourceRef] });
  const half = Number(fadeSeconds) / 2;
  const contributions = targetIds.map((frameId) => ({
    target: toTarget(frameId),
    role: frameId,
    kind: "media",
    ...media(frameId),
    ...(half > 0 ? { fade_in_seconds: half, fade_out_seconds: half } : {}),
    ...(keepLastPhoto ? { retain_on_expiry: true } : {}),
  }));
  const scene = { scene_id: sceneId, revision, cycle_seconds: cycleSeconds, loop, contributions };
  if (ending !== "none") {
    const seconds = Number(endingSeconds);
    scene.outro_seconds = seconds;
    scene.outro_contributions = targetIds.map((frameId) => (ending === "black"
      ? { target: toTarget(frameId), role: frameId, kind: "black" }
      : { target: toTarget(frameId), role: frameId, kind: "media", ...media(frameId),
          ...(half > 0 ? { fade_in_seconds: half } : {}),
          fade_out_seconds: seconds - half }));
  }
  if (keepTogether) {
    scene.protect_frames = true;
  }
  if (mode === "authored") {
    // The asset_ids list is the de-duplicated set of chosen refs the authored route persists.
    const assetIds = [...new Set(targetIds.map((frameId) => selections[frameId]))];
    return {
      path: `/v1/operator/scenes/${encodeURIComponent(sceneId)}/authored`,
      body: { scene, source_ref: sourceRef, asset_ids: assetIds },
    };
  }
  return { path: `/v1/operator/scenes/${encodeURIComponent(sceneId)}`, body: scene };
}

// --- Editing a stored Scene (§13): only when the round trip is lossless.

// central/runtime.py model defaults (`Scene`, `Contribution`): a stored Scene
// is compared with the console's rebuild of it after both are filled with
// these, so a field left at its default never withholds Edit. A pytest pins
// both literals to the models (tests/test_operator_runtime.py).
export const SCENE_DEFAULTS = {
  "revision": 1,
  "contributions": [],
  "children": [],
  "cycle_seconds": 30,
  "loop": false,
  "duration_seconds": null,
  "outro_seconds": 0,
  "outro_contributions": [],
  "protect_frames": false
};

export const CONTRIBUTION_DEFAULTS = {
  "kind": "media",
  "role": null,
  "source_refs": [],
  "asset_refs": [],
  "opacity": 1,
  "fade_in_seconds": 0,
  "fade_out_seconds": 0,
  "retain_on_expiry": false,
  "ramp_from": 0,
  "ramp_to": 0
};

export const UNAUTHORABLE_REASON =
  "Uses features the console can't edit yet (child Scenes, see-through photos, " +
  "different settings per Frame, or values outside its ranges).";

/** A Scene with every omitted Scene and Contribution field at its model default. */
export function normalizeScene(scene) {
  const contribution = (entry) => ({ ...CONTRIBUTION_DEFAULTS, ...entry });
  const filled = { ...SCENE_DEFAULTS, ...scene };
  return {
    ...filled,
    contributions: filled.contributions.map(contribution),
    outro_contributions: filled.outro_contributions.map(contribution),
  };
}

/** One JSON text per value, whatever the key order. */
function canonical(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/**
 * A stored Scene as the authoring form's draft: authored when its first
 * Contribution names assets, otherwise live from its first Source. An authored
 * Scene does not store its Source, so the operator picks it again.
 *
 * Delivery 1x's settings are read from the first Contribution (fade, keep the last photo)
 * and the first outro Contribution (how it ends); a Scene whose other Contributions differ
 * does not rebuild to itself, so {@link editableDraft} withholds Edit.
 *
 * @param {object} scene a served Scene definition
 * @returns {{mode: "live"|"authored", sourceRef: string, targetIds: string[],
 *            cycleSeconds: number, loop: boolean, selections: Record<string, string>,
 *            fadeSeconds: number, ending: Ending, endingSeconds: number,
 *            keepLastPhoto: boolean, keepTogether: boolean}}
 */
export function decodeScene(scene) {
  const {
    contributions, cycle_seconds: cycleSeconds, loop, outro_seconds: outroSeconds,
    outro_contributions: outro, protect_frames: keepTogether,
  } = normalizeScene(scene);
  const authored = contributions.length > 0 && contributions[0].asset_refs.length > 0;
  const frameIdOf = (entry) => frameOf(entry.target) ?? entry.target;
  const first = contributions[0];
  const ending = outroSeconds === 0 ? "none" : outro[0]?.kind === "black" ? "black" : "fade";
  return {
    mode: authored ? "authored" : "live",
    sourceRef: authored ? "" : (first?.source_refs[0] ?? ""),
    targetIds: contributions.map(frameIdOf),
    cycleSeconds,
    loop,
    selections: authored
      ? Object.fromEntries(contributions.map((entry) => [frameIdOf(entry), entry.asset_refs[0]]))
      : {},
    fadeSeconds: first ? first.fade_in_seconds + first.fade_out_seconds : 0,
    ending,
    endingSeconds: ending === "none" ? SCENE_SETTING_DEFAULTS.endingSeconds : outroSeconds,
    keepLastPhoto: first ? first.retain_on_expiry : SCENE_SETTING_DEFAULTS.keepLastPhoto,
    keepTogether,
  };
}

/**
 * The draft to edit a stored Scene with, or null when the console cannot
 * author it losslessly (§13): the Scene must equal `buildSave(decodeScene(it))`
 * apart from `revision`, both sides filled with the model defaults. Anything
 * else — child Scenes, opacity, unequal fades, settings that differ per Frame,
 * several Sources — would be dropped by a save, so Edit is withheld with
 * {@link UNAUTHORABLE_REASON}.
 *
 * @param {object} scene a served Scene definition
 * @returns {ReturnType<typeof decodeScene>|null}
 */
export function editableDraft(scene) {
  const draft = decodeScene(scene);
  // A value the sliders cannot show exactly (a 12 s ending, a 1.3 s fade) would be moved by
  // the first touch, so such a Scene is not the console's to edit.
  if (!inRange(draft.fadeSeconds, SETTING_RANGES.fadeSeconds)
      || (draft.ending !== "none" && !inRange(draft.endingSeconds, SETTING_RANGES.endingSeconds))) {
    return null;
  }
  const { body } = buildSave(draft.mode, { ...draft, sceneId: scene.scene_id });
  const rebuilt = draft.mode === "authored" ? body.scene : body;
  const comparable = (value) => canonical({ ...normalizeScene(value), revision: 0 });
  return comparable(rebuilt) === comparable(scene) ? draft : null;
}

/**
 * A Scene draft (flow design §7 J4): its kind (`mode`), name and id, Source, target
 * frames, per-frame choices, cycle and loop, delivery 1x's settings ({@link buildSave}),
 * and the stored `revision` it was seeded from (null for a new Scene; the flow's
 * `baseRevision`).
 *
 * @typedef {{mode: "live"|"authored", name: string, idOverride: string|null,
 *            sourceRef: string, targets: string[], selections: Record<string, string>,
 *            cycleSeconds: string|number, loop: boolean, fadeSeconds: number,
 *            ending: Ending, endingSeconds: number, keepLastPhoto: boolean,
 *            keepTogether: boolean, revision: number|null}} SceneDraft
 */

/**
 * A new Scene's defaults, each with its source: live from a Source; 30 s per
 * cycle; loop on, so a Scene keeps playing until its Program ends (slice 3 Question 1);
 * delivery 1x's settings at {@link SCENE_SETTING_DEFAULTS}.
 *
 * @type {Readonly<SceneDraft>}
 */
export const NEW_SCENE_DRAFT = Object.freeze({
  mode: "live",
  name: "",
  idOverride: null,
  sourceRef: "",
  targets: Object.freeze([]),
  selections: Object.freeze({}),
  cycleSeconds: 30,
  loop: true,
  ...SCENE_SETTING_DEFAULTS,
  revision: null,
});

/**
 * The draft to edit a stored Scene with, under its stored revision, or null when the
 * console cannot author it losslessly ({@link editableDraft}). The name is not
 * stored, so it stays empty: an edit keeps the stored id.
 *
 * @param {object} scene a served Scene definition
 * @returns {SceneDraft|null}
 */
export function sceneEditDraft(scene) {
  const draft = editableDraft(scene);
  if (draft === null) {
    return null;
  }
  const { targetIds, ...rest } = draft;
  return { ...NEW_SCENE_DRAFT, ...rest, targets: targetIds, revision: normalizeScene(scene).revision };
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

/** Scene, window and priority problems shared by one Program and the helper. */
function scheduleProblems({ sceneId, start, end, priority }, now, effectiveTimes = null) {
  const problems = [];
  if (sceneId === "") {
    problems.push({ field: "scene", message: "Choose a Scene." });
  }
  const startsAt = effectiveTimes?.startsAt ?? toEpochSeconds(start);
  const endsAt = effectiveTimes?.endsAt ?? toEpochSeconds(end);
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
export function programProblems(draft, existingIds, now, effectiveTimes = null) {
  return [...identityProblems("Program", draft, existingIds), ...scheduleProblems(draft, now, effectiveTimes)];
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

// The fields the helper's windows are planned from.
const PLANNED_FROM = new Set(["start", "end", "weekdays", "count"]);

/**
 * The helper's problems (§6 Windows): the Program problems without the base
 * id's collision (it is never stored), then the mask, the count; once the
 * windows can be planned (start, end, mask and count sound), no overlap; and
 * once the name gives an id too, each window id's length and collision. The
 * overlap does not wait for the name, so the step that asks the window (the
 * Schedule flow's When) can say so before the name is asked.
 *
 * @param {{name: string, idOverride: string|null, sceneId: string, start: string,
 *          end: string, priority: string|number, weekdays: boolean[],
 *          count: string|number}} draft
 * @param {Set<string>} existingIds the stored Program ids
 * @param {number} now
 * @returns {Problem[]}
 */
export function windowProblems(draft, existingIds, now) {
  const identity = identityProblems("Program", draft, new Set());
  const problems = [...identity, ...scheduleProblems(draft, now)];
  if (!draft.weekdays.some(Boolean)) {
    problems.push({ field: "weekdays", message: "Tick at least one weekday." });
  }
  const count = Number(draft.count);
  if (!(Number.isInteger(count) && count >= 1 && count <= MAX_WINDOWS)) {
    problems.push({ field: "count", message: `Between 1 and ${MAX_WINDOWS} windows.` });
  }
  if (problems.some((problem) => PLANNED_FROM.has(problem.field))) {
    return problems;
  }
  const windows = planWindows({ ...draft, count });
  if (windows.some((window, index) => index > 0 && windows[index - 1].endsAt > window.startsAt)) {
    problems.push({ field: "weekdays", message: "Each window must end before the next starts." });
  }
  if (identity.length > 0) {
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
  windows.forEach((_window, index) => {
    const windowId = `${id}-${index + 1}`;
    if (existingIds.has(windowId)) {
      problems.push({ field: idField, message: `${windowId} already exists.` });
    }
  });
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
 * @param {{sourceName: string, connectionRef: string, capturedFrom: string,
 *          capturedUntil: string}} draft
 * @returns {Problem[]}
 */
export function sourceProblems({ sourceName, connectionRef, capturedFrom, capturedUntil }) {
  const problems = [];
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$/.test(sourceName.trim())) {
    problems.push({
      field: "ref",
      message: "Choose a Source name of up to 120 letters, digits, hyphens, underscores or dots, starting with a letter or digit.",
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
    problems.push({ field: "until", message: "'Dated until' must be after 'Dated from'." });
  }
  return problems;
}
