/**
 * The draft of one flow as a pure value (flow design §4, §6): which instance is open,
 * its Plane B value, the value it was seeded with, and the stored revision it was
 * seeded from. No React; `useFlowDraft` holds one of these, and
 * tests/test_console_flow.py drives these functions under Node.
 *
 * A seed function `seed(key)` returns an instance's initial value: the defaults for
 * `new`, or the stored record for an edit. A seeded value's `revision` field, when it
 * has one, is the stored revision it came from; that is the draft's `baseRevision`
 * (null for a new instance). The draft never changes it: only a reseed does.
 *
 * INVARIANT: one draft per flow. `openDraft` of another key while the open draft is
 * dirty is refused: the state is unchanged and the OPEN key is returned, so the
 * caller asks the operator (Resume or Discard) before anything is lost.
 *
 * IDENTITY. Every draft `openDraft` seeds carries the `id` its caller mints (a number
 * never used before in that flow); keeping the same instance (the same key, a refused
 * key, a patch, a reseed) keeps it, and a closed draft has none. So the id tells one
 * opened draft from a later one of the same key: a new Scene begun after another was
 * discarded is keyed `new` too, but it is another draft.
 *
 * @typedef {{key: string|null, id: number|null, value: object|null, seeded: object|null,
 *            baseRevision: number|null}} DraftState
 */

/** @type {DraftState} */
export const CLOSED = Object.freeze({ key: null, id: null, value: null, seeded: null, baseRevision: null });

/**
 * Structural equality of two draft values (plain data: objects, arrays, primitives).
 * Object key order does not matter.
 *
 * @param {unknown} a
 * @param {unknown} b
 * @returns {boolean}
 */
export function sameValue(a, b) {
  if (Object.is(a, b)) {
    return true;
  }
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) {
    return false;
  }
  if (Array.isArray(a) !== Array.isArray(b)) {
    return false;
  }
  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  return (
    keysA.length === keysB.length &&
    keysA.every((key) => Object.prototype.hasOwnProperty.call(b, key) && sameValue(a[key], b[key]))
  );
}

/**
 * Whether the open draft differs from the value it was seeded with.
 *
 * @param {DraftState} state
 * @returns {boolean}
 */
export function isDirty(state) {
  return state.key !== null && !sameValue(state.value, state.seeded);
}

function seeded(key, id, seed) {
  const value = seed(key);
  const revision = value?.revision;
  return {
    key,
    id,
    value,
    seeded: value,
    baseRevision: typeof revision === "number" ? revision : null,
  };
}

/**
 * Open instance `key`. The same key keeps its draft; another key replaces a clean
 * draft (or none) with `seed(key)`, identified by `id`, and is refused while the open
 * draft is dirty.
 *
 * @param {DraftState} state
 * @param {string} key
 * @param {(key: string) => object} seed
 * @param {number} id the new draft's identity, if one is seeded: never used before
 * @returns {{state: DraftState, opened: string|null}} `opened` is `key` when it is
 *   now open, otherwise the key still open
 */
export function openDraft(state, key, seed, id) {
  if (state.key === key) {
    return { state, opened: key };
  }
  if (isDirty(state)) {
    return { state, opened: state.key };
  }
  return { state: seeded(key, id, seed), opened: key };
}

/**
 * Merge `partial` (or `partial(value)`) into the open draft's value. Returns the same
 * state when nothing is open or nothing changes, so a no-op patch never re-renders.
 *
 * @param {DraftState} state
 * @param {object|((value: object) => object|null)} partial
 * @returns {DraftState}
 */
export function patchDraft(state, partial) {
  if (state.key === null) {
    return state;
  }
  const changes = typeof partial === "function" ? partial(state.value) : partial;
  if (changes == null || Object.keys(changes).every((key) => sameValue(state.value[key], changes[key]))) {
    return state;
  }
  return { ...state, value: { ...state.value, ...changes } };
}

/**
 * Seed the open instance again from `seed` (Reload): its value, and its
 * `baseRevision`, become the current stored record's. It is the same draft (its `id`).
 *
 * @param {DraftState} state
 * @param {(key: string) => object} seed
 * @returns {DraftState}
 */
export function reseedDraft(state, seed) {
  return state.key === null ? state : seeded(state.key, state.id, seed);
}
