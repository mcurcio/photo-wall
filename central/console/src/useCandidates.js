import { useEffect, useState } from "react";

import { readCandidates } from "./candidatesApi.js";

const EMPTY = {};
const NONE = [];

/**
 * Each target Frame's candidates from one Source (Bead 14b), read through
 * candidatesApi.js `readCandidates`, which Central HARD-FILTERS by the photo fit
 * rule at the Frame's box on its screen — an asset that does not fit the Frame is
 * never returned, so it can never be offered. A Frame whose screen size is not known
 * yet (Central's 409 `frame_size_unknown`) has no candidates and is listed in
 * `unsized`; the other Frames' lists still load. Each candidate carries the planner's `standing` for its Frame.
 * `loading` until they are read (§6); `ready` once the lists for exactly this Source
 * and these Frames are read; `reload` reads them again.
 *
 * The Scene flow container calls this (flow design §7), so the read and the prune it
 * feeds run whichever step is showing.
 *
 * @param {string} sourceRef "" reads nothing
 * @param {string[]} targetIds
 * @param {number} catalogRevision the Source's served completed refresh revision
 * @param {number} [cropLimit] the Scene's crop limit (a fraction) Central judges each
 *   photo's fit at (roadmap 5a); Central's default when not given. A change reads again.
 * @returns {{byFrame: Record<string, Array<object>>, unsized: string[], loading: boolean,
 *            error: string|null, ready: boolean, reload: () => void}}
 */
export function useCandidates(sourceRef, targetIds, catalogRevision = 0, cropLimit = undefined) {
  const [nonce, setNonce] = useState(0);
  const targetKey = targetIds.join(" ");
  const key = sourceRef === "" || targetKey === "" ? "" :
    `${nonce}\n${sourceRef}\n${targetKey}\n${catalogRevision}\n${cropLimit ?? ""}`;
  const [loaded, setLoaded] = useState(
    /** @type {{key: string, byFrame: Record<string, Array<object>>, unsized: string[],
     *          error: string|null}} */ ({
      key: "",
      byFrame: {},
      unsized: [],
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
        const unsized = [];
        const entries = await Promise.all(
          frameIds.map(async (frameId) => {
            try {
              return [frameId, (await readCandidates(sourceRef, frameId, cropLimit)).candidates];
            } catch (error) {
              // A Frame whose screen size is not known yet has no shape to judge photos at
              // (decision 0030): no candidates for that Frame alone, never the Scene's error.
              if (error instanceof Error && error.message === "frame_size_unknown") {
                unsized.push(frameId);
                return [frameId, []];
              }
              throw error;
            }
          }),
        );
        if (!ignore) {
          setLoaded({ key, byFrame: Object.fromEntries(entries), unsized, error: null });
        }
      } catch {
        if (!ignore) {
          setLoaded({ key, byFrame: {}, unsized: [], error: "Could not load candidate media for these Frames." });
        }
      }
    })();
    return () => {
      ignore = true;
    };
    // `key` carries sourceRef, targetKey, the crop limit and the reload nonce.
  }, [key]);

  const current = key !== "" && loaded.key === key;
  return {
    byFrame: current ? loaded.byFrame : EMPTY,
    unsized: current ? loaded.unsized : NONE,
    loading: key !== "" && !current,
    error: current ? loaded.error : null,
    ready: current && loaded.error === null,
    reload: () => setNonce((value) => value + 1),
  };
}
