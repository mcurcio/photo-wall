import { useCallback, useEffect, useRef, useState } from "react";

import { draftPool, sourcePool } from "./photoSourcesApi.js";

/** How often the first page is re-read: while a check runs, and after it ends. */
export const CHECKING_POLL_MS = 2000;
export const SETTLED_POLL_MS = 30000;

/**
 * A draft's or a saved Source's pool as it fills (roadmap 4, K2): the first page re-read every
 * {@link CHECKING_POLL_MS} while its check is `checking` (or not started), then every
 * {@link SETTLED_POLL_MS}; later pages read on "Show more" from the last page's cursor. A
 * `cursor_invalid` answer restarts from the top. `restart()` drops every page (after the query
 * changed); a new `draftRef` or `sourceRef` restarts by itself.
 *
 * `view` is the first page's answer (its counts and check); `pages` every page's items, in
 * order. A photo that moved across a page boundary while the pool changed is shown once
 * (the pool view keys items by `asset_id`).
 *
 * @param {{draftRef: string|null, sourceRef: string|null}} target
 * @returns {{view: import("./photoSourcesApi.js").SourcePoolView|null,
 *            pages: Array<Array<import("./photoSourcesApi.js").PoolItem>>,
 *            hasMore: boolean, loadMore: () => Promise<void>, restart: () => void,
 *            error: string|null, loadingMore: boolean}}
 */
export function useDraftPool({ draftRef, sourceRef }) {
  const target = draftRef ?? sourceRef ?? null;
  const read = useCallback(
    (after) => (draftRef ? draftPool(draftRef, after) : sourcePool(sourceRef, after)),
    [draftRef, sourceRef],
  );
  const [pages, setPages] = useState([]);
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [epoch, setEpoch] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const cursors = useRef([]);          // the cursor after each page read, in order
  const [hasMore, setHasMore] = useState(false);
  const generation = useRef(0);

  const restart = useCallback(() => setEpoch((current) => current + 1), []);

  useEffect(() => {
    if (target === null) {
      setPages([]);
      setView(null);
      return undefined;
    }
    const mine = ++generation.current;
    cursors.current = [];
    setPages([]);
    setView(null);
    setError(null);
    setHasMore(false);
    let timer = null;
    const tick = async () => {
      const result = await read(null);
      if (generation.current !== mine) return;
      let delay = SETTLED_POLL_MS;
      if (result.ok) {
        setError(null);
        setView(result.pool);
        cursors.current[0] = result.pool.next;
        if (cursors.current.length === 1) setHasMore(result.pool.next !== null);
        setPages((current) => [result.pool.items, ...current.slice(1)]);
        const state = result.pool.check?.state;
        if (state === "checking" || state === "never_checked") delay = CHECKING_POLL_MS;
      } else {
        setError(result.error);
        delay = CHECKING_POLL_MS;
      }
      timer = setTimeout(tick, delay);
    };
    void tick();
    return () => {
      generation.current += 1;
      if (timer !== null) clearTimeout(timer);
    };
  }, [target, read, epoch]);

  const loadMore = useCallback(async () => {
    const after = cursors.current[cursors.current.length - 1];
    if (!after) return;
    const mine = generation.current;
    setLoadingMore(true);
    const result = await read(after);
    setLoadingMore(false);
    if (generation.current !== mine) return;
    if (!result.ok) {
      if (result.error === "cursor_invalid") restart();
      else setError(result.error);
      return;
    }
    cursors.current = [...cursors.current, result.pool.next];
    setHasMore(result.pool.next !== null);
    setPages((current) => [...current, result.pool.items]);
  }, [read, restart]);

  return { view, pages, hasMore, loadMore, restart, error, loadingMore };
}
