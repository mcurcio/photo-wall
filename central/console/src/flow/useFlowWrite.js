import { useRef, useState } from "react";

import { useMutate } from "../useMutate.js";

/**
 * A flow's final write (flow design §6 History, §7), written once for every flow: Save
 * Scene, Save source, Schedule Program and Activate now.
 *
 * SEND. `send(write)` checks every problem (the kit's `checkAll`; nothing is sent while
 * one remains, or while a write is in flight), clears the status line, marks the flow
 * `busy` and HOLDS its draft (useFlowDraft `hold`), then runs `write(sent)`. While the
 * draft is held it stays the open draft: no reseed or hand-over replaces it, a route
 * naming another instance shows "Resume or Discard", and Discard, New and Edit are
 * disabled (FlowFrame, and a flow's own cards, read `busy`). The step is read-only.
 *
 * THE ANSWER belongs to the draft it was sent from: `send` captures that draft's `id`.
 * `sent.finish(focusAfter?, result?)` releases the hold and ends the flow (the kit's
 * `finish`) only while that draft is still the open one (useFlowDraft `isOpen`: not once
 * the flow unmounted, as Log out does); otherwise it only reports. `sent.isOpen()` asks
 * the same for anything else a write would apply to the draft. The status line
 * (`sent.say`, `sent.refused`) is the section's, so it reports either way.
 *
 * REQUESTS. `sent.request(op)` runs `op` inside `useMutate()` (one Plane A refresh after
 * the write) and answers its result, or null when it threw or timed out;
 * `sent.incomplete()` says "<failure>: the request did not complete.", as does a write
 * that throws anyway. `sent.refused(result)` says "<failure>: <code>." for a refusal the
 * flow has no words of its own for.
 *
 * `bind(fn)` binds `fn` to the draft open now: it runs only while that draft is still the
 * open one (the Scene flow's Replace, whose write runs in its confirmation).
 *
 * @param {{flow: {checkAll: (summary?: () => HTMLElement|null) => boolean,
 *                 finish: (focusAfter?: (() => HTMLElement|null)|null, result?: object|null) => boolean},
 *          draft: {id: number|null, hold: (held: boolean) => void,
 *                  isOpen: (id: number|null) => boolean},
 *          confirm: {setStatus: (text: string|null) => void},
 *          failure: string}} options `failure` leads the words of a write that failed
 *   ("Could not save Scene")
 */
export function useFlowWrite({ flow, draft, confirm, failure }) {
  const mutate = useMutate();
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const release = () => {
    if (busyRef.current) {
      busyRef.current = false;
      draft.hold(false);
      setBusy(false);
    }
  };

  const send = async (write) => {
    if (busyRef.current || !flow.checkAll()) {
      return;
    }
    const id = draft.id;
    busyRef.current = true;
    setBusy(true);
    draft.hold(true);
    confirm.setStatus(null);
    const sent = {
      isOpen: () => draft.isOpen(id),
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
      finish: (focusAfter, result = null) => {
        release();
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

  return { busy, send, bind };
}
