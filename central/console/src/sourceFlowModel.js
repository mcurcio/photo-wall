/**
 * The Source flow's shape (console DDD §37, pass 5): its steps, which step asks each field
 * (`sourceFieldStep`), its instances and routes, its seed, the connection rule, the words
 * each answer is said in, the selection summary and the stored write body. Pure: no React;
 * tests/test_console_flow.py and tests/test_console_sources.py drive it under Node. The
 * Source's problems stay in authoring.js (`sourceProblems`).
 *
 * A Source is a saved live query with a plain operator name (its stored revision is hidden):
 * Photo Wall selects media that lives in the operator's photo library and never uploads,
 * edits or deletes anything there. Nothing here names a vendor.
 *
 * STEPS, one question each (R24): Which library connection? → Choose tags → Narrow it down
 * → Name this Source → Check your Source. The connection step is skipped when the
 * connection rule finds exactly one connection (it is prefilled, and sits under the Name
 * step's Advanced). A new Source opens at its first step; an edit opens at Review and
 * carries the stored revision for an optimistic write.
 */

import { localDayStart } from "./authoring.js";
import { editedId, flowKeys } from "./flow/instance.js";
import { namedSource, sourceDay, sourceName } from "./sourceNames.js";
import { datedWords, favouritesWords, kindsWords, tagWords, UNTAGGED_SELECTION } from "./sourceWords.js";

/** @typedef {import("./flow/steps.js").Step} Step */

const LIBRARY = Object.freeze({ id: "library", label: "Library" });
const TAGS = Object.freeze({ id: "tags", label: "Tags" });
const NARROW = Object.freeze({ id: "narrow", label: "Narrow" });
const NAME = Object.freeze({ id: "name", label: "Name" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

/** Every step, in order: Library → Tags → Narrow → Name → Review. */
export const SOURCE_STEPS = Object.freeze([LIBRARY, TAGS, NARROW, NAME, REVIEW]);
const ONE_CONNECTION_STEPS = Object.freeze([TAGS, NARROW, NAME, REVIEW]);

/** Each step's heading: the one question it asks (§37). */
export const SOURCE_HEADINGS = Object.freeze({
  library: "Which library connection?",
  tags: "Choose tags",
  narrow: "Narrow it down (optional)",
  name: "Name this Source",
  review: "Check your Source",
});

/** Whether the connection rule leaves the connection with nothing to ask (one value). */
const oneConnection = (rule) => rule.shown === "advanced";

/**
 * The steps under the connection rule: without the connection step when exactly one
 * connection is known.
 *
 * @param {ReturnType<typeof connectionRule>} rule
 * @returns {ReadonlyArray<Step>}
 */
export function sourceSteps(rule) {
  return oneConnection(rule) ? ONE_CONNECTION_STEPS : SOURCE_STEPS;
}

/**
 * Which step asks each field of authoring.js `sourceProblems` and the tag picker (flow
 * design §6): the connection on its own step, or under the Name step's Advanced when the
 * rule prefills the one connection.
 *
 * @param {ReturnType<typeof connectionRule>} rule
 * @returns {Readonly<Record<string, string>>}
 */
export function sourceFieldStep(rule) {
  return oneConnection(rule) ? ONE_CONNECTION_FIELD_STEP : SOURCE_FIELD_STEP;
}

const FIELDS = { tags: TAGS.id, type: NARROW.id, favorites: NARROW.id, from: NARROW.id,
  until: NARROW.id, ref: NAME.id };
const SOURCE_FIELD_STEP = Object.freeze({ connection: LIBRARY.id, ...FIELDS });
const ONE_CONNECTION_FIELD_STEP = Object.freeze({ ...FIELDS, connection: NAME.id });

const describe = { create: "a new Source", edit: (name) => `Source ${name}` };
const SOURCE_KEYS = flowKeys({ section: "sources", firstStep: { create: LIBRARY.id, edit: REVIEW.id }, describe });
const ONE_CONNECTION_KEYS = flowKeys({ section: "sources", firstStep: { create: TAGS.id, edit: REVIEW.id }, describe });

/**
 * The Source flow's instances and their routes (flow/instance.js `flowKeys`): a new
 * Source opens at the rule's first step.
 *
 * @param {ReturnType<typeof connectionRule>} rule
 * @returns {import("./flow/instance.js").FlowKeys}
 */
export function sourceKeys(rule) {
  return oneConnection(rule) ? ONE_CONNECTION_KEYS : SOURCE_KEYS;
}

/**
 * Each field's label: the control's accessible name, Review's answer label and the
 * Change link's name. They are the single form's labels (§3 rule 3).
 */
export const SOURCE_LABELS = Object.freeze({
  connection: "Connection name",
  tags: "Tags in your library",
  type: "Media type",
  favorites: "Favourites",
  from: "Dated from",
  until: "Dated until",
  ref: "Source name",
});

/**
 * The chooser's last choice (several connections), and the field it shows: a connection
 * no Source uses yet. The choice's value can never be a connection's name (an
 * Identifier starts with a letter or digit).
 */
export const ANOTHER_CONNECTION = Object.freeze({
  value: "*another",
  words: "Another connection…",
  label: "New connection name",
});

/** "Media type" choices, [value, words]; the first is the default. */
export const MEDIA_TYPE_CHOICES = Object.freeze([
  ["both", "Photos and videos"],
  ["image", "Photos only"],
  ["video", "Videos only"],
]);

/** "Favourites" choices, [value, words]; the first is the default. */
export const FAVOURITES_CHOICES = Object.freeze([
  ["any", "Any"],
  ["only", "Only favourites"],
  ["not", "Not favourites"],
]);

/**
 * A new Source's defaults (§37): no tags (everything in the library), photos and videos,
 * any favourites, no dates; the name and the connection are the operator's (the
 * connection rule may prefill it). `newConnection` is true once the chooser's "Another
 * connection…" is chosen: the connection is then typed.
 *
 * `tags` are the library tag ids the Source selects by (all of them, each with its
 * nested tags; at most 4). Their paths are not draft state: the flow learns them from
 * the library's tag list (TagCombobox.jsx), so naming a tag never dirties a draft.
 *
 * @typedef {{mediaType: string, favorites: "any"|"only"|"not", capturedFrom: string,
 *            capturedUntil: string, sourceName: string, connectionRef: string,
 *            newConnection: boolean, tags: string[]}} SourceDraft
 * @type {Readonly<SourceDraft>}
 */
export const NEW_SOURCE_DRAFT = Object.freeze({
  mediaType: MEDIA_TYPE_CHOICES[0][0],
  favorites: FAVOURITES_CHOICES[0][0],
  capturedFrom: "",
  capturedUntil: "",
  sourceName: "",
  connectionRef: "",
  newConnection: false,
  tags: Object.freeze([]),
});

/**
 * THE CONNECTION RULE (flow design §7 J5, step 2), over the worker's reported IDs:
 *
 *  - `field`: worker IDs are not available yet (older worker / not reported); saved
 *    Source refs guide the old manual-input behavior, explicitly marked uncertain;
 *  - `blocked`: worker reported no configured connections;
 *  - `advanced`: one connection is known; it is prefilled, the connection step is
 *    skipped and the field sits under the Name step's Advanced;
 *  - `chooser`: several known values, or an edit whose saved connection is no longer
 *    reported. Manual entry is offered only while the worker list is unavailable.
 *
 * @param {string[]|null|undefined} connectionIds null until worker has reported
 * @param {ReadonlyArray<{spec?: {connection_ref?: string}}>} sources legacy guidance
 * @param {string} selectedRef current draft selection, to detect a removed connection
 * @returns {{shown: "field"|"advanced"|"chooser"|"blocked", values: string[], prefill: string,
 *            reported: boolean, selectedUnavailable: boolean}}
 */
export function connectionRule(connectionIds, sources = [], selectedRef = "") {
  const reported = Array.isArray(connectionIds);
  const values = reported
    ? [...new Set(connectionIds.filter((ref) => typeof ref === "string" && ref !== ""))].sort()
    : [...new Set(sources.map((source) => source.spec?.connection_ref ?? "").filter((ref) => ref !== ""))].sort();
  const selectedUnavailable = reported && selectedRef !== "" && !values.includes(selectedRef);
  if (reported && values.length === 0) {
    return { shown: "blocked", values, prefill: "", reported, selectedUnavailable };
  }
  if (selectedUnavailable) {
    return { shown: "chooser", values, prefill: "", reported, selectedUnavailable };
  }
  if (values.length === 0) {
    return { shown: "field", values, prefill: "", reported, selectedUnavailable };
  }
  if (values.length === 1) {
    return { shown: "advanced", values, prefill: values[0], reported, selectedUnavailable };
  }
  return { shown: "chooser", values, prefill: "", reported, selectedUnavailable };
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
 * @param {string[]|null|undefined} connectionIds
 * @returns {(key: string) => SourceDraft}
 */
export function seedSource(sources, connectionIds) {
  return (key) => {
    const name = editedId(key);
    if (name === null) {
      return { ...NEW_SOURCE_DRAFT, connectionRef: connectionRule(connectionIds, sources).prefill };
    }
    const source = namedSource(sources, name);
    if (!source) return NEW_SOURCE_DRAFT;
    const spec = source.spec ?? {};
    const kinds = spec.media_types ?? ["image", "video"];
    return {
      mediaType: kinds.length === 1 ? kinds[0] : "both",
      favorites: spec.favorites === true ? "only" : spec.favorites === false ? "not" : "any",
      capturedFrom: sourceDay(spec.captured_from),
      capturedUntil: sourceDay(spec.captured_until),
      sourceName: sourceName(source),
      connectionRef: spec.connection_ref ?? "",
      newConnection: false,
      tags: Array.isArray(spec.tags) ? [...spec.tags] : [],
      revision: source.revision ?? Number(source.source_ref?.match(/:([0-9]+)$/)?.[1] ?? 1),
    };
  };
}

const words = (choices, value) => choices.find(([choice]) => choice === value)?.[1] ?? value;

/** How Review says an empty capture day: the window is open on that side. */
export const NO_DAY_LIMIT = "No limit";

/**
 * Whether the draft's connection is one the media worker announced: only then can the
 * flow ask the library for its tags and a preview (§37). A typed name, or a name from
 * saved Sources before the worker reported its list, is unannounced.
 *
 * @param {ReturnType<typeof connectionRule>} rule
 * @param {string} connectionRef
 * @returns {boolean}
 */
export function connectionAnnounced(rule, connectionRef) {
  return rule.reported && rule.values.includes(connectionRef.trim());
}

/** What the tag and preview steps say when the worker's reported list excludes the connection (§37). */
export const UNANNOUNCED_CONNECTION =
  "This connection isn't set up yet, so tags and previews aren't available. You can still " +
  "save; the Source starts selecting once the connection is set up.";

/** What they say while the media worker has never reported its connections (§37; C5). */
export const UNREPORTED_CONNECTIONS =
  "The media worker hasn't reported its library connections yet, so tags and previews aren't " +
  "available yet. You can still save.";

/**
 * The tag and preview steps' sentence for an unannounced connection (§37), the one home of
 * both: "isn't set up yet" only once the worker has reported a list that excludes it.
 *
 * @param {ReturnType<typeof connectionRule>} rule
 * @returns {string}
 */
export function unannouncedWords(rule) {
  return rule.reported ? UNANNOUNCED_CONNECTION : UNREPORTED_CONNECTIONS;
}

/**
 * Review's answers in step order, each `{label, field, value}` (value: words, or null
 * when a required answer is not given). The connection is first on its own step, or after
 * the name when it sits under the Name step's Advanced.
 *
 * @param {SourceDraft} draft
 * @param {ReturnType<typeof connectionRule>} [rule]
 * @param {Readonly<Record<string, string|null>>} [tagPaths]
 * @returns {Array<{label: string, field: string, value: string|null}>}
 */
export function sourceAnswers(draft, rule = null, tagPaths = {}) {
  const given = (text) => (text.trim() === "" ? null : text.trim());
  const connection = ["connection", given(draft.connectionRef)];
  const tags = draft.tags ?? [];
  return [
    ...(rule !== null && oneConnection(rule) ? [] : [connection]),
    ["tags", tags.length === 0 ? NO_TAGS : tags.map((tag) => tagWords(tag, tagPaths)).join(" and ")],
    ["type", words(MEDIA_TYPE_CHOICES, draft.mediaType)],
    ["favorites", words(FAVOURITES_CHOICES, draft.favorites)],
    ["from", given(draft.capturedFrom) ?? NO_DAY_LIMIT],
    ["until", given(draft.capturedUntil) ?? NO_DAY_LIMIT],
    ["ref", given(draft.sourceName)],
    ...(rule !== null && oneConnection(rule) ? [connection] : []),
  ].map(([field, value]) => ({ label: SOURCE_LABELS[field], field, value }));
}

/** How Review says no tags: the Source takes everything the other criteria allow. */
export const NO_TAGS = `No tags: ${UNTAGGED_SELECTION}`;

/**
 * What a Source selects, in words (§39 selection summary, a `set` fact: the Source's own
 * spec): "Selects media tagged Family/Christmas (and nested tags) · favourites only ·
 * photos and videos · dated 3 Mar 2025 to 5 Mar 2025". Its pieces live in sourceWords.js,
 * the one home `sourceState` shares, so both say "dated", never "taken".
 *
 * @param {{tags?: string[], media_types?: string[], favorites?: boolean|null,
 *          captured_from?: number|null, captured_until?: number|null}|null} query the
 *   stored `SourceSpec` (or a draft's `buildSourceSpec` body)
 * @param {Readonly<Record<string, string|null>>} tagPaths id -> path (null: gone)
 * @returns {string}
 */
export function selectionWords(query, tagPaths = {}) {
  const tags = query?.tags ?? [];
  const named = tags.map((tag) => tagWords(tag, tagPaths));
  return [
    tags.length === 0 ? `Selects ${UNTAGGED_SELECTION}`
      : tags.length === 1 ? `Selects media tagged ${named[0]} (and nested tags)`
        : `Selects media tagged with all of ${named.join(", ")} (each with its nested tags)`,
    favouritesWords(query?.favorites),
    kindsWords(query?.media_types),
    datedWords(query?.captured_from, query?.captured_until),
  ].filter((part) => part !== null).join(" · ");
}


const FAVOURITES = { any: null, only: true, not: false };

/**
 * The stored `SourceSpec` write body (media/models.py `SourceSpec`) for a saved live
 * logical Source write body: `expected_revision`, its `connection_ref`, the
 * `media_types` subset, and the filters the API accepts:
 * `favorites` (Any / Only / Not → omitted / true / false) and the capture window
 * (`captured_from` / `captured_until`, the local midnights of the chosen days; "until"
 * is exclusive), and `tags` (library tag ids). An unset filter is omitted, so the server
 * applies its default and an untagged Source stores exactly as before tags existed.
 *
 * "both" maps to the full ["image", "video"] subset; otherwise the one chosen kind.
 * There is deliberately no vendor field: a Source is a saved live query over the library.
 *
 * @param {{connectionRef: string, mediaType: string, expectedRevision?: number|null,
 *          newName?: string|null, originalCapturedFrom?: number|null,
 *          originalCapturedUntil?: number|null,
 *          favorites?: "any"|"only"|"not", capturedFrom?: string,
 *          capturedUntil?: string, tags?: string[]}} draft
 * @returns {object}
 */
export function buildSourceSpec({
  connectionRef,
  mediaType,
  expectedRevision = null,
  newName = null,
  originalCapturedFrom = undefined,
  originalCapturedUntil = undefined,
  favorites = "any",
  capturedFrom = "",
  capturedUntil = "",
  tags = [],
}) {
  const spec = {
    expected_revision: expectedRevision,
    connection_ref: connectionRef,
    media_types: mediaType === "both" ? ["image", "video"] : [mediaType],
  };
  if (newName !== null) spec.new_name = newName;
  if (FAVOURITES[favorites] !== null) {
    spec.favorites = FAVOURITES[favorites];
  }
  const from = originalCapturedFrom === undefined ? localDayStart(capturedFrom) : originalCapturedFrom;
  const until = originalCapturedUntil === undefined ? localDayStart(capturedUntil) : originalCapturedUntil;
  if (from !== null) {
    spec.captured_from = from;
  }
  if (until !== null) {
    spec.captured_until = until;
  }
  if (tags.length > 0) {
    spec.tags = [...tags];
  }
  return spec;
}
