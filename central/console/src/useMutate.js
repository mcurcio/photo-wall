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
 * The write runs first; then Plane A is refreshed once and the write's result is
 * returned. If the write rejects, the error propagates and Plane A is left
 * untouched (the caller owns conflict/error handling).
 *
 * The write's result is independent of the refresh (slice 2 §7): a refresh that
 * fails after a successful write is NOT the write's failure. It shows only as
 * the provider's "last refresh failed", and the next poll catches up.
 *
 * @returns {<T>(op: () => Promise<T>) => Promise<T>}
 */
export function useMutate() {
  const { refresh } = useSnapshot();
  return useCallback(
    async (op) => {
      const result = await op();
      try {
        await refresh();
      } catch {
        // Surfaced by the provider's refreshFailed flag; never the write's outcome.
      }
      return result;
    },
    [refresh],
  );
}
