import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The Scene handed over to a flow that schedules or shows one (flow design §7 J6, J7):
 * the shell's `recentScene` (`{sceneId, seq}`), set when the operator saves a Scene or
 * picks one on a Scene card ("Schedule it", "Show now"). A new draft is seeded with it (the flow's
 * seed); this is the rule for one that is already open when another is handed over,
 * written once for the Schedule and Show-now flows:
 *
 *  - a draft that may follow it (clean, and not `held`) is reseeded with it;
 *  - otherwise the draft is kept: a draft without a Scene takes it (`choose`), and one
 *    naming another Scene is OFFERED it (`offered`), which the Scene step offers
 *    instead (`take`, and the kit's `OfferedScene` notice).
 *
 * `held` keeps a clean draft too (Show now's, while its last outcome is unknown: its
 * activation key must stay for the retry), and so does a draft whose write is in flight
 * (useFlowDraft `held`: the answer belongs to it). `accepts(sceneId)` says whether the Scene can
 * be taken at all (Show now's: it is still stored); a Scene it refuses is ignored.
 * Only a new hand-over (a new `seq`) asks for any of this: handing over the Scene a
 * changed draft was offered before, or already handed, offers it again. `clear()` forgets the offer
 * (another instance opened, or the flow's write done).
 *
 * @param {{recentScene: {sceneId: string, seq: number}|null,
 *          draft: {key: string|null, dirty: boolean, held: boolean, reseed: () => void},
 *          sceneId: string, held?: boolean, accepts?: (sceneId: string) => boolean,
 *          choose: (sceneId: string) => void}} options
 * @returns {{offered: string|null, take: () => void, clear: () => void}}
 */
export function useSceneHandOver({
  recentScene,
  draft,
  sceneId,
  held = false,
  accepts = () => true,
  choose,
}) {
  const [offered, setOffered] = useState(/** @type {string|null} */ (null));
  const handedRef = useRef(recentScene?.seq ?? null);

  useEffect(() => {
    const seq = recentScene?.seq ?? null;
    if (handedRef.current === seq) {
      return;
    }
    handedRef.current = seq;
    const recentSceneId = recentScene?.sceneId ?? null;
    if (recentSceneId === null || draft.key === null || !accepts(recentSceneId)) {
      return;
    }
    if (!draft.dirty && !held && !draft.held) {
      if (sceneId !== recentSceneId) {
        draft.reseed();
      }
      setOffered(null);
    } else if (sceneId === "") {
      choose(recentSceneId);
      setOffered(null);
    } else {
      setOffered(recentSceneId);
    }
  });

  const clear = useCallback(() => setOffered(null), []);
  const take = () => {
    if (offered !== null) {
      choose(offered);
    }
    setOffered(null);
  };

  return { offered: offered !== null && offered !== sceneId ? offered : null, take, clear };
}
