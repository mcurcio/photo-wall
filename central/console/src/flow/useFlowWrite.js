import { useRef, useState } from "react";

import { useMutate } from "../useMutate.js";

/**
 * A flow's final write (flow design §6 History, §7), written once for every flow: Save
 * Scene, Save source, Schedule Program and Activate now.
 *
 * SEND. `send(write)` checks every problem (the kit's `checkAll`; nothing is sent while
 * one remains, or while a write is in flight), clears the status line and marks the flow
 * `busy` (the step is read-only: FlowFrame reads it), then runs `write(sent)`.
 * `sent.finish(focusAfter?, result?)` ends the flow (the kit's `finish`). The status line
 * (`sent.say`, `sent.refused`) is the section's.
 *
 * REQUESTS. `sent.request(op)` runs `op` inside `useMutate()` (one Plane A refresh after
 * the write) and answers its result, or null when it threw or timed out;
 * `sent.incomplete()` says "<failure>: the request did not complete.", as does a write
 * that throws anyway. `sent.refused(result)` says "<failure>: <code>." for a refusal the
 * flow has no words of its own for.
 *
 * @param {{flow: {checkAll: (summary?: () => HTMLElement|null) => boolean,
 *                 finish: (focusAfter?: (() => HTMLElement|null)|null, result?: object|null) => boolean},
 *          confirm: {setStatus: (text: string|null) => void},
 *          failure: string}} options `failure` leads the words of a write that failed
 *   ("Could not save Scene")
 */
export function useFlowWrite({ flow, confirm, failure }) {
  const mutate = useMutate();
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const release = () => {
    if (busyRef.current) {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const send = async (write) => {
    if (busyRef.current || !flow.checkAll()) {
      return;
    }
    busyRef.current = true;
    setBusy(true);
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
      finish: (focusAfter, result = null) => {
        release();
        flow.finish(focusAfter, result);
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

  return { busy, send };
}
