/**
 * A flow's instances and where the route puts them (flow design §6, §7), as pure
 * functions: no React. `useFlowInstance` builds on them, and
 * tests/test_console_flow.py drives them under Node.
 *
 * INSTANCES. A flow's draft is keyed `new`, or `edit/<id>` for the stored record it
 * edits. (The prefix keeps a record whose id is "new" apart from a new one; ids never
 * hold a `/`.) A flow's {@link FlowKeys} map keys to routes and back: `new` is
 * `#/<section>/<newFlow>/<step>`, an edit `#/<section>/<id>/edit/<step>`.
 *
 * PLACES. What a flow's section shows for the route (see {@link instancePlace}):
 * `list` (the section itself, or another section), `missing` or `unavailable` (an
 * edit whose record is gone, or that the console cannot author), `open` (the draft's
 * own instance), `blocked` (another instance while the draft is dirty: "Resume or
 * Discard") or `opening` (another instance, the draft clean or closed: it opens).
 *
 * @typedef {import("../routes.js").Route} Route
 * @typedef {"list"|"missing"|"unavailable"|"open"|"blocked"|"opening"} Place
 * @typedef {"ok"|"missing"|"unavailable"} Availability
 * @typedef {{section: string, fromRoute: (route: Route|null) => string|null,
 *            toRoute: (key: string, step: string) => Route,
 *            firstStep: (key: string) => string, describe: (key: string) => string}} FlowKeys
 */

export const NEW_KEY = "new";

const EDIT_PREFIX = "edit/";

/**
 * The stored id an edit key names, or null for a new instance (or none).
 *
 * @param {string|null} key
 * @returns {string|null}
 */
export function editedId(key) {
  return key?.startsWith(EDIT_PREFIX) ? key.slice(EDIT_PREFIX.length) : null;
}

/**
 * The key of the edit of stored record `id`.
 *
 * @param {string} id
 * @returns {string}
 */
export function editKey(id) {
  return `${EDIT_PREFIX}${id}`;
}

/**
 * A flow's keys over its routes.
 *
 * @param {{section: string, newFlow?: string, firstStep: {create: string, edit?: string},
 *          describe: {create: string, edit?: (id: string) => string}}} shape
 *   `newFlow` is the route segment of a new instance ("new"; Show now's is "show");
 *   `firstStep` is where each kind of instance opens; `describe` names it in words.
 *   A flow without `edit` has no edit routes.
 * @returns {FlowKeys}
 */
export function flowKeys({ section, newFlow = "new", firstStep, describe }) {
  const edits = firstStep.edit !== undefined;
  return Object.freeze({
    section,
    fromRoute(route) {
      if (route?.section !== section) {
        return null;
      }
      if (route.flow === newFlow) {
        return NEW_KEY;
      }
      return edits && route.flow === "edit" ? editKey(route.id) : null;
    },
    toRoute(key, step) {
      const id = editedId(key);
      return id === null
        ? { section, flow: newFlow, step }
        : { section, id, flow: "edit", step };
    },
    firstStep(key) {
      return editedId(key) === null ? firstStep.create : firstStep.edit;
    },
    describe(key) {
      const id = editedId(key);
      return id === null ? describe.create : describe.edit(id);
    },
  });
}

/**
 * What the section shows for the route (see PLACES above).
 *
 * @param {{routeKey: string|null, available: Availability, draftKey: string|null,
 *          dirty: boolean}} state
 * @returns {Place}
 */
export function instancePlace({ routeKey, available, draftKey, dirty }) {
  if (routeKey === null) {
    return "list";
  }
  if (available !== "ok") {
    return available;
  }
  if (routeKey === draftKey) {
    return "open";
  }
  return dirty ? "blocked" : "opening";
}

/**
 * The step shown: the route's step when the draft is open there and the step is one
 * of its steps, otherwise null (the route is normalised to a real step).
 *
 * @param {Place} place
 * @param {ReadonlyArray<{id: string}>} steps
 * @param {string|undefined} routeStep
 * @returns {string|null}
 */
export function shownStep(place, steps, routeStep) {
  return place === "open" && steps.some((step) => step.id === routeStep) ? routeStep : null;
}
