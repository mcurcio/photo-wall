import { useMemo, useRef, useState } from "react";

import { useMutate } from "../useMutate.js";
import { sameValue } from "./draftState.js";

/** What a flow says of ids a write sent that Central did not answer (NOT CONFIRMED). */
export const NOT_CONFIRMED = "may have been saved: Central did not answer.";

/**
 * A flow's final write (flow design §6 History, §7), written once for every flow: Save
 * Scene, Save source, Schedule Program and Activate now.
 *
 * SEND. `send(flow, write)` checks every problem (the kit's `flow.checkAll`; nothing is
 * sent while one remains, or while a write is in flight), clears the status line, marks
 * the flow `busy` and HOLDS its draft (useFlowDraft `hold`), then runs `write(sent)`.
 * While the draft is held it stays the open draft: no reseed or hand-over replaces it, a
 * route naming another instance shows "Resume or Discard", and Discard, New and Edit are
 * disabled (FlowFrame, and a flow's own cards, read `busy`). The step is read-only.
 *
 * THE ANSWER belongs to the draft it was sent from: `send` captures that draft's `id`.
 * `sent.finish(focusAfter?, result?)` releases the hold and ends the flow (`flow.finish`)
 * only while that draft is still the open one (useFlowDraft `isOpen`: not once the flow
 * unmounted, as Log out does); otherwise it only reports. The status line (`sent.say`,
 * `sent.refused`) is the section's, so it reports either way.
 *
 * REQUESTS. `sent.request(op)` runs `op` inside `useMutate()` (one Plane A refresh after
 * the write) and answers its result, or null when it threw or timed out;
 * `sent.incomplete()` says "<failure>: the request did not complete.", as does a write
 * that throws anyway. `sent.refused(result)` says "<failure>: <code>." for a refusal the
 * flow has no words of its own for.
 *
 * NOT CONFIRMED. A write Central did not answer (no answer, or a 5xx) may still have been
 * stored, and a partial one (the Schedule flow's separate windows) stored some of its
 * ids. `sent.attempted(ids, confirmed?)` remembers the ids this write sent, and those of
 * them it confirmed, with the draft's value: while that same draft is open and
 * unchanged, `attempt` is `{ids, confirmed}`, so the flow leaves those ids out of its
 * collision check (its own earlier attempt is not a collision) and sending the draft
 * again confirms them (each write here is an idempotent `PUT` for an identical body). A
 * change to the draft, another draft, or the flow's end forgets it. The hook needs no
 * flow until `send`, so a flow's problems can read `attempt`.
 *
 * `bind(fn)` binds `fn` to the draft open now: it runs only while that draft is still the
 * open one (the Scene flow's Replace, whose write runs in its confirmation).
 *
 * @param {{draft: {id: number|null, value: object|null, hold: (held: boolean) => void,
 *                  isOpen: (id: number|null) => boolean},
 *          confirm: {setStatus: (text: string|null) => void},
 *          failure: string}} options `failure` leads the words of a write that failed
 *   ("Could not save Scene")
 */
export function useFlowWrite({ draft, confirm, failure }) {
  const mutate = useMutate();
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [attempt, setAttempt] = useState(
    /** @type {{draftId: number|null, value: object|null, ids: string[], confirmed: string[]}|null} */ (
      null
    ),
  );

  const release = () => {
    if (busyRef.current) {
      busyRef.current = false;
      draft.hold(false);
      setBusy(false);
    }
  };

  /**
   * @param {{checkAll: () => boolean,
   *          finish: (focusAfter?: (() => HTMLElement|null), result?: object|null) => boolean}} flow
   * @param {(sent: object) => Promise<void>} write
   */
  const send = async (flow, write) => {
    if (busyRef.current || !flow.checkAll()) {
      return;
    }
    const id = draft.id;
    const value = draft.value;
    busyRef.current = true;
    setBusy(true);
    draft.hold(true);
    confirm.setStatus(null);
    const sent = {
      request: async (op) => {
        try {
          return await mutate(op);
        } catch {
          return null;
        }
      },
      say: (text) => confirm.setStatus(text),
      incomplete: () => confirm.setStatus(`${failure}: the request did not complete.`),
      refused: (result) => confirm.setStatus(`${failure}: ${result.error ?? `HTTP ${result.status}`}.`),
      attempted: (ids, confirmed = []) => {
        if (draft.isOpen(id)) {
          setAttempt({ draftId: id, value, ids: [...ids], confirmed: [...confirmed] });
        }
      },
      finish: (focusAfter, result = null) => {
        release();
        setAttempt(null);
        if (draft.isOpen(id)) {
          flow.finish(focusAfter, result);
        }
      },
    };
    try {
      await write(sent);
    } catch {
      confirm.setStatus(`${failure}: the request did not complete.`);
    } finally {
      release();
    }
  };

  const bind = (fn) => {
    const id = draft.id;
    return (...args) => (draft.isOpen(id) ? fn(...args) : undefined);
  };

  const current = useMemo(
    () =>
      attempt !== null && attempt.draftId === draft.id && sameValue(attempt.value, draft.value)
        ? { ids: attempt.ids, confirmed: attempt.confirmed }
        : null,
    [attempt, draft.id, draft.value],
  );

  return { busy, send, bind, attempt: current };
}
