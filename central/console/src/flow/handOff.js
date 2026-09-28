/**
 * An inline hand-off from one flow to another (flow design §7 J4 step 2: "New selection
 * from your photo library runs J5 inline and returns"), as pure values: no React.
 * `useHandOff` (the shell's) holds one; tests/test_console_flow.py drives these under Node.
 *
 * THE CONTRACT. The ORIGIN flow (the Scene flow) begins a hand-off to a TARGET section
 * (`to`, "sources") with a `label` for its own draft ("your Scene") and an `onReturn`
 * callback. The target flow (useFlowInstance's `handOff` option) opens its new
 * instance when a hand-off addressed to it begins, and SETTLES it exactly once:
 *
 *  - after its write, with the flow's result (the Source flow's `{sourceRef}`);
 *  - on leaving without one (Back on its first step, or discarding its draft), with null.
 *
 * Settling clears the hand-off and calls `onReturn(result, {show})`. `show` is true when
 * the target flow was on screen, so the origin shows its draft again (its step, focus
 * on the field the result fills); false when the operator had already gone elsewhere,
 * so the origin only takes the result. `onReturn` answers whether the origin took it:
 * false when the draft it was begun for is gone, and the target then ends as it would
 * without a hand-off. Until it is settled a hand-off stays pending (the target section
 * says so); beginning another replaces it, and the replaced one never returns.
 *
 * @typedef {{id: number, from: string, to: string, label: string}} HandOff
 * @typedef {{show: boolean}} ReturnOptions
 */

/**
 * The hand-off that begins `request` with id `id`; it replaces any pending one.
 *
 * @param {{from: string, to: string, label: string}} request
 * @param {number} id a new id, never used before
 * @returns {HandOff}
 */
export function beginHandOff({ from, to, label }, id) {
  return Object.freeze({ id, from, to, label });
}

/**
 * Settle hand-off `id`: `{next, settled}`, where `settled` says whether `id` was the
 * pending one (only then is it cleared and its origin called).
 *
 * @param {HandOff|null} current
 * @param {number} id
 * @returns {{next: HandOff|null, settled: boolean}}
 */
export function settleHandOff(current, id) {
  return current !== null && current.id === id
    ? { next: null, settled: true }
    : { next: current, settled: false };
}

/**
 * The pending hand-off addressed to `section`, or null.
 *
 * @param {HandOff|null} current
 * @param {string} section
 * @returns {HandOff|null}
 */
export function handOffTo(current, section) {
  return current !== null && current.to === section ? current : null;
}
