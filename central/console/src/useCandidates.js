import { useEffect, useState } from "react";

import { readCandidates } from "./candidatesApi.js";

const EMPTY = {};

/**
 * Each target Frame's candidates from one Source (Bead 14b), read through
 * candidatesApi.js `readCandidates`, which Central HARD-FILTERS by the Frame's
 * profile — an asset ineligible for a Frame's profile is never returned, so it can
 * never be offered. Each candidate carries the planner's `standing` for its Frame.
 * `loading` until they are read (§6); `ready` once the lists for exactly this Source
 * and these Frames are read; `reload` reads them again.
 *
 * The Scene flow container calls this (flow design §7), so the read and the prune it
 * feeds run whichever step is showing.
 *
 * @param {string} sourceRef "" reads nothing
 * @param {string[]} targetIds
 * @returns {{byFrame: Record<string, Array<object>>, loading: boolean,
 *            error: string|null, ready: boolean, reload: () => void}}
 */
export function useCandidates(sourceRef, targetIds) {
  const [nonce, setNonce] = useState(0);
  const targetKey = targetIds.join(" ");
  const key = sourceRef === "" || targetKey === "" ? "" : `${nonce}\n${sourceRef}\n${targetKey}`;
  const [loaded, setLoaded] = useState(
    /** @type {{key: string, byFrame: Record<string, Array<object>>, error: string|null}} */ ({
      key: "",
      byFrame: {},
      error: null,
    }),
  );

  useEffect(() => {
    if (key === "") {
      return undefined;
    }
    let ignore = false;
    const frameIds = targetKey.split(" ");
    (async () => {
      try {
        const entries = await Promise.all(
          frameIds.map(async (frameId) => [
            frameId,
            (await readCandidates(sourceRef, frameId)).candidates,
          ]),
        );
        if (!ignore) {
          setLoaded({ key, byFrame: Object.fromEntries(entries), error: null });
        }
      } catch {
        if (!ignore) {
          setLoaded({ key, byFrame: {}, error: "Could not load candidate media for these Frames." });
        }
      }
    })();
    return () => {
      ignore = true;
    };
    // `key` carries sourceRef, targetKey and the reload nonce.
  }, [key]);

  const current = key !== "" && loaded.key === key;
  return {
    byFrame: current ? loaded.byFrame : EMPTY,
    loading: key !== "" && !current,
    error: current ? loaded.error : null,
    ready: current && loaded.error === null,
    reload: () => setNonce((value) => value + 1),
  };
}
