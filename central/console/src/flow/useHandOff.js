import { useCallback, useMemo, useRef, useState } from "react";

import { beginHandOff, handOffTo, settleHandOff } from "./handOff.js";

/**
 * The shell's one pending inline hand-off between flows (flow/handOff.js has the
 * contract). The shell calls this once and passes the result to the Show sections'
 * flows through the route context (`handOffs`): the origin calls `begin`, the target
 * reads its hand-off with {@link useHandOffTo}. Log out remounts the shell and drops it.
 *
 * `begin({from, to, label, onReturn})` replaces any pending one;
 * `settle(id, result, {show})` clears hand-off `id` and returns what its `onReturn`
 * answered (false when `id` is no longer the pending one).
 *
 * @returns {{current: import("./handOff.js").HandOff|null,
 *            begin: (request: {from: string, to: string, label: string,
 *                    onReturn: (result: object|null, options: import("./handOff.js").ReturnOptions) => boolean}) => void,
 *            settle: (id: number, result: object|null, options: import("./handOff.js").ReturnOptions) => boolean}}
 */
export function useHandOff() {
  const [current, setCurrent] = useState(/** @type {import("./handOff.js").HandOff|null} */ (null));
  // Read synchronously by `settle`, so a hand-off settles once however fast it is asked.
  const currentRef = useRef(current);
  const returnRef = useRef(/** @type {Function|null} */ (null));
  const lastIdRef = useRef(0);

  const begin = useCallback(({ onReturn, ...request }) => {
    lastIdRef.current += 1;
    const next = beginHandOff(request, lastIdRef.current);
    currentRef.current = next;
    returnRef.current = onReturn;
    setCurrent(next);
  }, []);

  const settle = useCallback((id, result, options) => {
    const { next, settled } = settleHandOff(currentRef.current, id);
    if (!settled) {
      return false;
    }
    const onReturn = returnRef.current;
    currentRef.current = next;
    returnRef.current = null;
    setCurrent(next);
    return onReturn(result, options) === true;
  }, []);

  return useMemo(() => ({ current, begin, settle }), [current, begin, settle]);
}

/**
 * The hand-off addressed to `section`, with its `settle(result, options)` bound, for
 * useFlowInstance's `handOff` option; null when none is pending.
 *
 * @param {ReturnType<typeof useHandOff>|null|undefined} handOffs
 * @param {string} section
 * @returns {(import("./handOff.js").HandOff & {settle: (result: object|null,
 *            options: import("./handOff.js").ReturnOptions) => boolean})|null}
 */
export function useHandOffTo(handOffs, section) {
  const mine = handOffTo(handOffs?.current ?? null, section);
  const settle = handOffs?.settle;
  return useMemo(
    () => (mine === null ? null : { ...mine, settle: (result, options) => settle(mine.id, result, options) }),
    [mine, settle],
  );
}
