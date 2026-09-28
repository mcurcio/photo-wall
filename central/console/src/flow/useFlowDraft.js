import { useCallback, useRef, useState } from "react";

import { CLOSED, isDirty, openDraft, patchDraft, reseedDraft } from "./draftState.js";

/**
 * One flow's draft (flow design §6 frozen surface): the Plane B values of ONE flow
 * instance, keyed `new` or by the stored record it edits.
 *
 * The flow container that calls this never unmounts (rule 2), so only Save, Discard,
 * Log out (which remounts the shell) or a reload end a draft. The rules are the pure
 * ones in draftState.js:
 *
 *  - `open(key)` opens an instance and returns the key now open. While the open draft
 *    is `dirty`, another key is REFUSED and the open key is returned, so the caller
 *    asks first (one draft per flow).
 *  - `patch(partial)` merges values; `partial` may be a function of the current value,
 *    so effects (prunes) compose with the operator's edits.
 *  - `reseed()` re-runs `seed(key)` against the current snapshot and resets
 *    `baseRevision` (Reload); `discard()` closes the draft.
 *  - `baseRevision` is the stored revision the draft was seeded from (null for new).
 *
 * `seed` is read when it is called, so it may close over the latest snapshot.
 *
 * @template T
 * @param {(key: string) => T} seed
 * @returns {{key: string|null, value: T|null, open: (key: string) => string|null,
 *            patch: (partial: Partial<T>|((value: T) => Partial<T>|null)) => void,
 *            reseed: () => void, discard: () => void, dirty: boolean,
 *            baseRevision: number|null}}
 */
export function useFlowDraft(seed) {
  const seedRef = useRef(seed);
  seedRef.current = seed;
  // The state lives in a ref so `open` can answer synchronously and functional
  // patches from several effects compose; the counter re-renders after a change.
  const stateRef = useRef(CLOSED);
  const [, setVersion] = useState(0);

  const commit = useCallback((next) => {
    if (next !== stateRef.current) {
      stateRef.current = next;
      setVersion((version) => version + 1);
    }
  }, []);

  const open = useCallback(
    (key) => {
      const { state, opened } = openDraft(stateRef.current, key, (k) => seedRef.current(k));
      commit(state);
      return opened;
    },
    [commit],
  );
  const patch = useCallback((partial) => commit(patchDraft(stateRef.current, partial)), [commit]);
  const reseed = useCallback(
    () => commit(reseedDraft(stateRef.current, (k) => seedRef.current(k))),
    [commit],
  );
  const discard = useCallback(() => commit(CLOSED), [commit]);

  const state = stateRef.current;
  return {
    key: state.key,
    value: state.value,
    open,
    patch,
    reseed,
    discard,
    dirty: isDirty(state),
    baseRevision: state.baseRevision,
  };
}
