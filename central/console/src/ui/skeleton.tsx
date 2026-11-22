import { cn } from "./cn";

export interface SkeletonProps {
  /** `block` fills its box (a tile, a card); `line` is one line of text. */
  shape?: "block" | "line";
}

/**
 * Skeleton (design language §3, the template contract): a grey block in the shape of what is being
 * read, never a spinner over a blank page. Decorative: the region that holds it says it is busy.
 * It pulses unless the person asked for less motion.
 */
export function Skeleton({ shape = "block" }: SkeletonProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "block rounded-card bg-surface-sunken motion-safe:animate-pulse",
        shape === "block" ? "size-full min-h-24" : "h-4 w-40 max-w-full",
      )}
    />
  );
}
