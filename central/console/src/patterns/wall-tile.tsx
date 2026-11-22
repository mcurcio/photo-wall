import type * as React from "react";

import type { Severity } from "../design/tokens";
import { cn } from "../ui/cn";
import { Menu, type MenuItem } from "../ui/menu";
import { StatusChip } from "../ui/status-chip";
import { AckBadge } from "./ack-badge";

export interface WallTileProps {
  /** The Frame's name: the tile's heading and its link's words. */
  name: string;
  /** The Frame's page: a tap anywhere on the tile but its menu opens it. */
  href: string;
  /** Called on a plain tap, before the link is followed (the page it leads to takes focus). */
  onOpen?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
  /** Its status, already judged and worded: the one chip. */
  chip: { severity: Severity; label: string };
  /** What it is doing, in one line ("Evenings · until 23:00"); none: nothing is said. */
  line: string | null;
  /** The last read is not current: the tile is drawn sunk and dashed and says since when ("Can't tell since 21:04"). */
  stale?: string;
  /** An action asked of it that its Pi has not acknowledged yet ("Restart requested"). */
  requested?: string;
  /** Its actions; none for a Frame still in setup. An action that is not offered is absent. `label`
   * names the "More" button for what it acts on, and holds the word More ("More actions for Hall"). */
  menu?: { label: string; items: readonly MenuItem[]; as?: "auto" | "popup" | "sheet" };
}

/**
 * WallTile (design language §4): one Frame on a wall, read-only: its name, its status chip, what
 * it is doing in one line and its actions behind "More". The whole tile opens the Frame's page
 * (its name is the link, stretched over the tile); the menu sits above that. Its status is the
 * chip's words, never a colour of the tile, and a tile whose read is not current is sunk and dashed
 * with since when, never drawn as if it were.
 */
export function WallTile({ name, href, onOpen, chip, line, stale, requested, menu }: WallTileProps) {
  return (
    <div
      role="group"
      aria-label={name}
      className={cn(
        "relative flex min-w-0 flex-col gap-1.5 rounded-card border p-3 text-sm text-text focus-within:border-line-input",
        // Not current: sunk and dashed, never a severity colour and never fainter text.
        stale === undefined ? "border-line bg-surface-raised" : "border-dashed border-line-input bg-surface-sunken",
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-2">
        <a
          href={href}
          onClick={onOpen}
          className={cn(
            "min-w-0 font-medium wrap-anywhere text-text no-underline after:absolute after:inset-0 after:rounded-card",
            "focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2",
            "focus-visible:after:outline-focus",
          )}
        >
          {name}
        </a>
        {menu !== undefined && menu.items.length > 0 && (
          <span className="relative z-10 -mt-1.5 -mr-2 shrink-0">
            <Menu label={menu.label} items={menu.items} as={menu.as} />
          </span>
        )}
      </div>
      <span className="flex min-w-0 flex-wrap items-center gap-1.5">
        <StatusChip severity={chip.severity}>{chip.label}</StatusChip>
      </span>
      {line !== null && (
        <span className="line-clamp-2 min-w-0 text-xs wrap-anywhere text-muted">
          {line}
        </span>
      )}
      {stale !== undefined && <span className="text-xs text-muted">{stale}</span>}
      {requested !== undefined && <span><AckBadge state="requested" words={requested} /></span>}
    </div>
  );
}
