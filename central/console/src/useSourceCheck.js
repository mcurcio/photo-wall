import { useCallback, useEffect, useRef, useState } from "react";

import { words as codeWords } from "./facts.js";
import { checkSource } from "./photoSourcesApi.js";
import { useMutate } from "./useMutate.js";

/**
 * Check now's request and its receipt: one home (roadmap 4, run 2), for the Source page, the
 * Scene editor and Show now. The receipt shows until Central serves a check that differs from the
 * one the request was made from; then the caller shows the Source's state in words
 * (mediaHealth.js `sourceState`). It never compares with the browser's clock.
 *
 * @typedef {{kind: "requested"|"unknown"|"failed", before: object|null, message: string}} CheckFeedback
 */

export const CHECK_REQUESTED = "Check requested. Photos keep playing while Photo Wall reads Immich.";
export const CHECK_UNKNOWN = "The request's outcome is unknown. Look at the Source's state before asking again.";

/**
 * POST /v1/operator/sources/{ref}/checks (photoSourcesApi.checkSource) as feedback.
 *
 * @param {{ok: boolean, status: number, error?: string|null}|null} result
 * @param {object|null} before the Source's `check` when the request was made
 * @returns {CheckFeedback}
 */
export function sourceCheckResult(result, before) {
  if (result?.ok && result.status === 202) {
    return { kind: "requested", before, message: CHECK_REQUESTED };
  }
  if (result == null || result.status === 0 || result.status >= 500) {
    return { kind: "unknown", before, message: CHECK_UNKNOWN };
  }
  return {
    kind: "failed",
    before,
    message: `Check request failed: ${result.error ? codeWords(result.error) : `HTTP ${result.status}`}.`,
  };
}

/** Whether two served checks are the same one: its state and its own times. */
function sameCheck(a, b) {
  if (a == null || b == null) return a == null && b == null;
  return a.state === b.state && (a.started_at ?? null) === (b.started_at ?? null) && (a.at ?? null) === (b.at ?? null);
}

/**
 * The receipt until the served `check` differs from `feedback.before` (state, started_at or at);
 * then null, and the caller shows sourceState's words. Never compares with the browser's clock.
 *
 * @param {CheckFeedback|null|undefined} feedback
 * @param {object|null|undefined} check
 * @returns {string|null}
 */
export function sourceCheckMessage(feedback, check) {
  if (feedback == null) return null;
  return sameCheck(feedback.before, check ?? null) ? feedback.message : null;
}

/**
 * Scene- or page-scoped Check now: {pending, feedback, run(sourceRef, before)}. A scope change
 * drops old feedback and in-flight results; a transport error refreshes the snapshot once
 * (useMutate) before calling the outcome unknown. One request at a time per scope.
 *
 * @param {unknown} scope
 * @returns {{pending: string|null, feedback: Record<string, CheckFeedback>,
 *            run: (sourceRef: string, before: object|null) => Promise<void>}}
 */
export function useSourceCheck(scope) {
  const mutate = useMutate();
  const serial = useRef(0);
  const activeScope = useRef(scope);
  const inFlight = useRef(null);
  const [pending, setPending] = useState(null);
  const [feedback, setFeedback] = useState({});

  useEffect(() => {
    activeScope.current = scope;
    serial.current += 1;
    inFlight.current = null;
    setPending(null);
    setFeedback({});
  }, [scope]);

  const run = useCallback(async (sourceRef, before) => {
    if (inFlight.current?.scope === scope) return;
    const requestId = ++serial.current;
    inFlight.current = { scope, requestId };
    setPending({ scope, sourceRef });
    setFeedback((current) => {
      const { [sourceRef]: _dropped, ...rest } = current;
      return rest;
    });
    try {
      const result = await mutate(() => checkSource(sourceRef));
      if (serial.current !== requestId || activeScope.current !== scope) return;
      setFeedback((current) => ({ ...current, [sourceRef]: sourceCheckResult(result, before) }));
    } finally {
      if (inFlight.current?.requestId === requestId) inFlight.current = null;
      if (serial.current === requestId && activeScope.current === scope) setPending(null);
    }
  }, [mutate, scope]);

  return {
    run,
    pending: pending?.scope === scope ? pending.sourceRef : null,
    feedback: activeScope.current === scope ? feedback : {},
  };
}
