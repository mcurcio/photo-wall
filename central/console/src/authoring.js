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
