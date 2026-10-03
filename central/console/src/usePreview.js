import { useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { failurePhase, pollDelay, SETTLE_MS, STILL_LOOKING_MS } from "./sourcePreview.js";

const IDLE = Object.freeze({ phase: "idle", answer: null, previous: null, stillLooking: false, code: null, connection: "" });

/**
 * What a Source draft selects, kept for the open draft (console DDD §37, §40): one preview
 * request per criteria, re-asked on each change (R23). The words are sourcePreview.js's.
 *
 * `key` names the criteria (and the draft); null asks nothing (no announced connection).
 * A new key waits {@link SETTLE_MS} for the next change, then POSTs
 * `/v1/operator/source-previews` with `payload` and polls `GET …/{request_id}` at 2 s,
 * doubling to 30 s. After 2 min without an answer it is `stillLooking`. A request the
 * library could not reach (or that expired, or Central lost) is asked again on the same
 * schedule; a refused key or another failure is final for those criteria.
 *
 * SEQUENCE. Every run of the request loop takes the next sequence number, and the hook's
 * cleanup (a criteria change, the panel leaving the screen) moves the sequence on, so an
 * answer that arrives for an earlier run is dropped: only the current run writes state.
 * The earlier answer itself stays on screen as `previous` beside "Updating…" or a failure.
 *
 * `active` is false while no panel is shown (the Library and Name steps): nothing is
 * polled, the state is kept, and on return an accepted request is polled again, never
 * posted twice. The container never unmounts, so neither does this state.
 *
 * @param {{key: string|null, payload: object, active: boolean, connection: string}} options
 * @returns {import("./sourcePreview.js").Preview}
 */
export function usePreview({ key, payload, active, connection }) {
  const [state, setState] = useState(IDLE);
  const sequence = useRef(0);
  const job = useRef(/** @type {null|{key: string, requestId: string|null, startedAt: number|null,
    attempt: number, done: boolean, body: object}} */ (null));
  const sent = useRef(payload);
  sent.current = payload;

  useEffect(() => {
    const mine = ++sequence.current;
    const current = () => sequence.current === mine;
    if (key === null) {
      job.current = null;
      setState((previous) => ({ ...IDLE, previous: previous.answer ?? previous.previous }));
      return () => { sequence.current += 1; };
    }
    if (job.current?.key !== key) {
      job.current = { key, requestId: null, startedAt: null, attempt: 0, done: false, body: sent.current };
      setState((previous) => ({ ...IDLE, phase: "looking", connection,
        previous: previous.answer ?? previous.previous }));
    }
    const run = job.current;
    if (!active || run.done) {
      return () => { sequence.current += 1; };
    }
    if (run.startedAt !== null) run.attempt = 0; // back on screen: read it again soon
    let timer = null;
    const sleep = (ms) => new Promise((resolve) => { timer = window.setTimeout(resolve, ms); });
    const publish = (patch) => {
      if (current()) setState((previous) => ({ ...previous, ...patch }));
    };
    const finish = (patch) => {
      run.done = true;
      publish({ ...patch, stillLooking: false });
    };
    const loop = async () => {
      if (run.startedAt === null) {
        await sleep(SETTLE_MS);
        if (!current()) return;
        run.startedAt = performance.now();
      }
      while (current()) {
        if (performance.now() - run.startedAt >= STILL_LOOKING_MS) publish({ stillLooking: true });
        if (run.requestId === null) {
          const posted = await apiWrite("/v1/operator/source-previews", { method: "POST", body: run.body })
            .catch(() => null);
          if (!current()) return;
          if (posted?.status === 202 && posted.data?.request_id) {
            run.requestId = posted.data.request_id; // the waits keep backing off across asks
          } else if (posted !== null && posted.status >= 400 && posted.status < 500 && posted.status !== 429) {
            finish({ phase: "failed", code: posted.error ?? `http_${posted.status}` });
            return;
          } else {
            await sleep(pollDelay(run.attempt++)); // lost, Central busy or at its pending cap
            continue;
          }
        }
        await sleep(pollDelay(run.attempt++));
        if (!current()) return;
        const poll = await apiWrite(`/v1/operator/source-previews/${encodeURIComponent(run.requestId)}`,
          { method: "GET" }).catch(() => null);
        if (!current()) return;
        if (poll === null || poll.status >= 500) continue; // the same request, read again
        if (poll.status === 404) {
          run.requestId = null; // Central no longer has it: ask again
          continue;
        }
        if (!poll.ok) {
          finish({ phase: "failed", code: poll.error ?? `http_${poll.status}` });
          return;
        }
        if (poll.data?.status === "complete") {
          finish({ phase: "complete", answer: poll.data });
          return;
        }
        if (poll.data?.status === "failed") {
          const phase = failurePhase(poll.data.error);
          if (phase !== "unreachable") {
            finish({ phase, code: poll.data.error ?? null });
            return;
          }
          publish({ phase: "unreachable", code: poll.data.error });
          run.requestId = null; // asked again after the next wait
          await sleep(pollDelay(run.attempt++));
        }
      }
    };
    loop();
    return () => {
      sequence.current += 1;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [key, active, connection]);

  return state;
}
