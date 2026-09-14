import { useCallback } from "react";

import { useSnapshot } from "./useSnapshot.js";

/**
 * Refresh-after-mutate hook (shared primitive #7).
 *
 * Returns a function that runs an operator write and then refreshes Plane A
 * EXACTLY ONCE, so the surface reflects the new server state through the same
 * snapshot every region reads. Every frontend mutation bead (8, 9, 10, 11, 13,
 * 14, 15, 16) uses this instead of hand-rolling a post-write refresh; Bead 18
 * only adds focus/visibility + a clock and does NOT retrofit prior beads.
 *
 * The write runs first; on success Plane A is refreshed once and the write's
 * result is returned. If the write rejects, the error propagates and Plane A is
 * left untouched (the caller owns conflict/error handling).
 *
 * @returns {<T>(op: () => Promise<T>) => Promise<T>}
 */
export function useMutate() {
  const { refresh } = useSnapshot();
  return useCallback(
    async (op) => {
      const result = await op();
      await refresh();
      return result;
    },
    [refresh],
  );
}
