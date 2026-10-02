import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The console's one polled read (console DDD §28): a value re-read every `cadenceMs` while
 * the tab is visible, at once when it becomes visible again, and on demand (`refresh`).
 *
 * `load(previous)` returns the next value from the previous one and must not throw: it
 * folds a failed read into the value it returns (a node read keeps its last records and
 * names its error beside them). Reads are single flight; a refresh asked for while one is
 * in flight is queued, not dropped, because that read may have started before the write it
 * follows. `latest()` returns the newest settled value at call time, from a ref, so a send
 * judges what arrived even before React re-renders. `skip` reads nothing and keeps the
 * value it has.
 *
 * @template T
 * @param {(previous: T) => Promise<T>} load a stable callback (useCallback)
 * @param {{cadenceMs: number, skip?: boolean, initial: T}} options
 * @returns {{value: T, refresh: () => Promise<void>, latest: () => T}}
 */
export function usePolledRead(load, { cadenceMs, skip = false, initial }) {
  const [value, setValue] = useState(initial);
  const newest = useRef(initial);
  const inFlight = useRef(false);
  const again = useRef(false);
  const alive = useRef(true);

  const readOnce = useCallback(async () => {
    const next = await load(newest.current);
    if (!alive.current) return;
    // The ref is the one source of truth; state follows it for rendering.
    newest.current = next;
    setValue(next);
  }, [load]);
  const latest = useCallback(() => newest.current, []);

  const refresh = useCallback(async () => {
    if (inFlight.current) {
      again.current = true;
      return;
    }
    inFlight.current = true;
    try {
      do {
        again.current = false;
        await readOnce();
      } while (again.current && alive.current);
    } finally {
      inFlight.current = false;
    }
  }, [readOnce]);

  useEffect(() => {
    alive.current = true;
    if (skip) return () => { alive.current = false; };
    const tick = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    tick();
    const timer = setInterval(tick, cadenceMs);
    document.addEventListener("visibilitychange", tick);
    return () => {
      alive.current = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [refresh, cadenceMs, skip]);

  return { value, refresh: skip ? noRefresh : refresh, latest };
}

const noRefresh = async () => {};
