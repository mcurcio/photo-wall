import { useCallback, useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { codeWords } from "./mediaHealth.js";
import { sourceRefreshPath } from "./mediaApi.js";
import { useMutate } from "./useMutate.js";

/** One Source refresh request receipt; the worker's completed Source status stays authoritative. */
export function sourceRefreshResult(result) {
  if (result.ok && result.status === 202) {
    return {
      kind: "requested",
      requestedRevision: result.data?.requested_revision ?? null,
      message: "Refresh requested. The status will update when the media worker finishes.",
    };
  }
  if (result.status === 0 || result.status >= 500) {
    return {
      kind: "unknown",
      requestedRevision: null,
      message: "The refresh request outcome is unknown. Check the Source status before retrying.",
    };
  }
  return {
    kind: "failed",
    requestedRevision: null,
    message: `Refresh request failed: ${result.error ? codeWords(result.error) : `HTTP ${result.status}`}.`,
  };
}

/** Once a requested revision completes, replace the receipt with the served Source status. */
export function sourceRefreshMessage(feedback, source, state) {
  if (feedback == null) return null;
  const completed = feedback.kind === "requested" && source !== null &&
    feedback.requestedRevision != null &&
    Number(source.refresh_completed_revision ?? 0) >= feedback.requestedRevision;
  if (completed) return `Refresh finished. Current Source status: ${state.label}.`;
  return feedback.message;
}

/**
 * Scene-scoped refresh of a saved Source. Changing the Scene or its draft scope
 * invalidates old feedback and in-flight results; a transport error still causes
 * one snapshot refresh through useMutate before its outcome is called unknown.
 */
export function useSourceRefresh(scope) {
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

  const run = useCallback(async (sourceRef) => {
    if (inFlight.current?.scope === scope) return null;
    const requestId = ++serial.current;
    inFlight.current = { scope, requestId };
    setPending({ scope, sourceRef });
    setFeedback((current) => ({ ...current, [sourceRef]: null }));
    try {
      const result = await mutate(async () => {
        try {
          return await apiWrite(sourceRefreshPath(sourceRef), { method: "POST" });
        } catch {
          return { ok: false, status: 0, error: null, data: null };
        }
      });
      if (serial.current !== requestId || activeScope.current !== scope) return result;
      setFeedback((current) => ({ ...current, [sourceRef]: sourceRefreshResult(result) }));
      return result;
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
