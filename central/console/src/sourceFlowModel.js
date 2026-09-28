/**
 * The Source flow's shape (flow design §7 J5, "Add a photo source"): its steps, which
 * step asks each field (`FIELD_STEP`), its instances and routes, its seed, the
 * connection rule, the words each answer is said in, and the stored write body. Pure:
 * no React; tests/test_console_flow.py drives it under Node. The Source's problems
 * stay in authoring.js (`sourceProblems`).
 *
 * A Source is a saved live query named `name:rev` (design D-e): Photo Wall selects
 * media that lives in the photo library and never uploads, edits or deletes anything
 * there. Nothing here names a vendor or an album.
 *
 * INSTANCES. Only a new Source is authored here (a Source is never edited in place: a
 * change is a new revision), so the one key is `new`, opening at "What to include".
 */

import { localDayStart } from "./authoring.js";
import { flowKeys } from "./flow/instance.js";

/** @typedef {import("./flow/steps.js").Step} Step */

const INCLUDE = Object.freeze({ id: "include", label: "What to include" });
const NAME = Object.freeze({ id: "name", label: "Name" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

/** The steps, in order: What to include → Name → Review. */
export const SOURCE_STEPS = Object.freeze([INCLUDE, NAME, REVIEW]);

/** Which step asks each field of authoring.js `sourceProblems` (flow design §6). */
export const SOURCE_FIELD_STEP = Object.freeze({
  type: INCLUDE.id,
  favorites: INCLUDE.id,
  from: INCLUDE.id,
  until: INCLUDE.id,
  ref: NAME.id,
  connection: NAME.id,
});

/** The Source flow's instances and their routes (flow/instance.js `flowKeys`). */
export const SOURCE_KEYS = flowKeys({
  section: "sources",
  firstStep: { create: INCLUDE.id },
  describe: { create: "a new photo source" },
});

/**
 * Each field's label: the control's accessible name, Review's answer label and the
 * Change link's name. They are the single form's labels (§3 rule 3).
 */
export const SOURCE_LABELS = Object.freeze({
  type: "Media type",
  favorites: "Favourites",
  from: "Taken from",
  until: "Taken until",
  ref: "Source name and revision",
  connection: "Connection name",
});

/** "Media type" choices, [value, words]; the first is the default. */
export const MEDIA_TYPE_CHOICES = Object.freeze([
  ["both", "Images and video"],
  ["image", "Images only"],
  ["video", "Video only"],
]);

/** "Favourites" choices, [value, words]; the first is the default. */
export const FAVOURITES_CHOICES = Object.freeze([
  ["any", "Any"],
  ["only", "Only favourites"],
  ["not", "Not favourites"],
]);

/**
 * A new Source's defaults, each with its source: images and video, any favourites
 * (the single form's, `SourcesRegion.jsx:157-158` before bead 3), no capture window;
 * the name and the connection are the operator's (the connection rule may prefill it).
 *
 * @typedef {{mediaType: string, favorites: "any"|"only"|"not", capturedFrom: string,
 *            capturedUntil: string, sourceRef: string, connectionRef: string}} SourceDraft
 * @type {Readonly<SourceDraft>}
 */
export const NEW_SOURCE_DRAFT = Object.freeze({
  mediaType: MEDIA_TYPE_CHOICES[0][0],
  favorites: FAVOURITES_CHOICES[0][0],
  capturedFrom: "",
  capturedUntil: "",
  sourceRef: "",
  connectionRef: "",
});

/**
 * THE CONNECTION RULE (flow design §7 J5, step 2), over the served Sources:
 *
 *  - `field`: no Source names a connection yet; "Connection name" is a visible,
 *    required text field, empty;
 *  - `advanced`: every Source's served `spec` has the same one `connection_ref`; it is
 *    prefilled and the field sits under the Name step's Advanced;
 *  - `chooser`: several values; a visible chooser of them, with none chosen.
 *
 * @param {ReadonlyArray<{spec?: {connection_ref?: string}}>} sources
 * @returns {{shown: "field"|"advanced"|"chooser", values: string[], prefill: string}}
 */
export function connectionRule(sources) {
  const values = [
    ...new Set(sources.map((source) => source.spec?.connection_ref ?? "").filter((ref) => ref !== "")),
  ].sort();
  if (values.length === 0) {
    return { shown: "field", values, prefill: "" };
  }
  if (values.length === 1) {
    return { shown: "advanced", values, prefill: values[0] };
  }
  return { shown: "chooser", values, prefill: "" };
}

const NO_ADVANCED = Object.freeze(new Set());
const CONNECTION_ADVANCED = Object.freeze(new Set(["connection"]));

/**
 * The fields that sit under their step's Advanced under `rule`: the connection in the
 * one-value case, nothing otherwise.
 *
 * @param {ReturnType<typeof connectionRule>} rule
 * @returns {ReadonlySet<string>}
 */
export function sourceAdvancedFields(rule) {
  return rule.shown === "advanced" ? CONNECTION_ADVANCED : NO_ADVANCED;
}

/**
 * The Source flow's seed over the served Sources: the defaults, with the connection
 * the rule prefills.
 *
 * @param {ReadonlyArray<object>} sources
 * @returns {(key: string) => SourceDraft}
 */
export function seedSource(sources) {
  return () => ({ ...NEW_SOURCE_DRAFT, connectionRef: connectionRule(sources).prefill });
}

const words = (choices, value) => choices.find(([choice]) => choice === value)?.[1] ?? value;

/** How Review says an empty capture day: the window is open on that side. */
export const NO_DAY_LIMIT = "No limit";

/**
 * Review's answers in step order, each `{label, field, value}` (value: words, or null
 * when a required answer is not given).
 *
 * @param {SourceDraft} draft
 * @returns {Array<{label: string, field: string, value: string|null}>}
 */
export function sourceAnswers(draft) {
  const given = (text) => (text.trim() === "" ? null : text.trim());
  return [
    ["type", words(MEDIA_TYPE_CHOICES, draft.mediaType)],
    ["favorites", words(FAVOURITES_CHOICES, draft.favorites)],
    ["from", given(draft.capturedFrom) ?? NO_DAY_LIMIT],
    ["until", given(draft.capturedUntil) ?? NO_DAY_LIMIT],
    ["ref", given(draft.sourceRef)],
    ["connection", given(draft.connectionRef)],
  ].map(([field, value]) => ({ label: SOURCE_LABELS[field], field, value }));
}

const FAVOURITES = { any: null, only: true, not: false };

/**
 * The stored `SourceSpec` write body (media/models.py `SourceSpec`) for a saved live
 * query: `schema` (alias of schema_version), the `source_ref` (`name:rev`), its
 * `connection_ref`, the `media_types` subset, and the filters the API accepts:
 * `favorites` (Any / Only / Not → omitted / true / false) and the capture window
 * (`captured_from` / `captured_until`, the local midnights of the chosen days; "until"
 * is exclusive). An unset filter is omitted, so the server applies its default.
 *
 * "both" maps to the full ["image", "video"] subset; otherwise the one chosen kind.
 * There is deliberately no album or vendor field: a Source is a saved live query, and
 * `SourceSpec` has no album filter.
 *
 * @param {{sourceRef: string, connectionRef: string, mediaType: string,
 *          favorites?: "any"|"only"|"not", capturedFrom?: string,
 *          capturedUntil?: string}} draft
 * @returns {object}
 */
export function buildSourceSpec({
  sourceRef,
  connectionRef,
  mediaType,
  favorites = "any",
  capturedFrom = "",
  capturedUntil = "",
}) {
  const spec = {
    schema: 1,
    source_ref: sourceRef,
    connection_ref: connectionRef,
    media_types: mediaType === "both" ? ["image", "video"] : [mediaType],
  };
  if (FAVOURITES[favorites] !== null) {
    spec.favorites = FAVOURITES[favorites];
  }
  const from = localDayStart(capturedFrom);
  const until = localDayStart(capturedUntil);
  if (from !== null) {
    spec.captured_from = from;
  }
  if (until !== null) {
    spec.captured_until = until;
  }
  return spec;
}
