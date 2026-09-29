import { useCallback, useRef, useState } from "react";

import { useMutate } from "./useMutate.js";

/**
 * A guarded mutation for actions attached to list cards. Duplicate calls for the
 * same key are ignored, and every resolved or unknown request gets one snapshot
 * refresh through useMutate. Callers own the endpoint and its response wording.
 *
 * @returns {{pending: Set<string>, feedback: Record<string, any>,
 *            run: (key: string, request: () => Promise<any>,
 *              describe: (result: any) => any) => Promise<any>,
 *            retain: (keys: Set<string>) => void}}
 */
export function useCardMutation() {
  const mutate = useMutate();
  const inFlight = useRef(new Set());
  const [pending, setPending] = useState(() => new Set());
  const [feedback, setFeedback] = useState({});

  const run = useCallback(async (key, request, describe) => {
    if (inFlight.current.has(key)) return null;
    inFlight.current.add(key);
    setPending(new Set(inFlight.current));
    setFeedback((current) => ({ ...current, [key]: null }));
    try {
      // A transport rejection has an unknown outcome. Return a normalized value
      // so useMutate still refreshes Plane A before the caller reports it.
      const result = await mutate(async () => {
        try {
          return await request();
        } catch {
          return { ok: false, status: 0, error: null, data: null };
        }
      });
      setFeedback((current) => ({ ...current, [key]: describe(result) }));
      return result;
    } finally {
      inFlight.current.delete(key);
      setPending(new Set(inFlight.current));
    }
  }, [mutate]);

  const retain = useCallback((keys) => {
    setFeedback((current) => {
      const stale = Object.keys(current).filter((key) => !keys.has(key));
      if (stale.length === 0) return current;
      const next = { ...current };
      stale.forEach((key) => { delete next[key]; });
      return next;
    });
  }, []);

  return { pending, feedback, run, retain };
}
