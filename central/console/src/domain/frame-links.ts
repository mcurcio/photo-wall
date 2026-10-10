import type { OwnerLink } from "../patterns/link-to-owner";
import { formatRoute } from "../routes.js";

/**
 * A Pi's bound Frames as links to their Frame page's Hardware tab (design rule H1). Hardware does
 * not own a Frame's setup state, so these are plain links with no severity.
 */
export function frameLinks(frames: readonly { frameId: string }[]): OwnerLink[] {
  return frames.map((entry) => ({
    text: `Frame ${entry.frameId}`,
    href: formatRoute({ section: "wall", id: entry.frameId, tab: "hardware" }),
  }));
}
