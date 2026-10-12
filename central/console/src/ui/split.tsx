import type * as React from "react";

import { cn } from "./cn";

export interface SplitProps {
  /** What the line says: a title, a count line, a message. It wraps between words. */
  children: React.ReactNode;
  /** The trailing action ("Show ›", a "More" menu, a checkbox, Close, Undo); none draws the
   * content alone. */
  action: React.ReactNode;
  /** `span` inside phrasing content (a button's label); `div` otherwise. */
  as?: "div" | "span";
  /** How the two align on one line: `center` for a single line, `start` for a block. */
  align?: "center" | "start";
}

/**
 * Split (design language §4): content and its trailing action on one line, the action at the
 * end. The action never shrinks and never breaks inside a word, even under a container that
 * lets text wrap anywhere (`overflow-wrap: anywhere` shrinks a flex item's minimum to one
 * letter, which drew "Sho / w ›" and "Mor / e"); when the line is too narrow for the content's
 * readable width and the action side by side, the action moves to a row of its own, at the end.
 */
export function Split({ children, action, as: Element = "div", align = "center" }: SplitProps) {
  return (
    <Element className={cn("flex w-full min-w-0 flex-wrap justify-between gap-x-3 gap-y-1",
      align === "center" ? "items-center" : "items-start")}>
      <Element className="block min-w-0 grow basis-48">{children}</Element>
      {action === null || action === undefined || action === false ? null : (
        <Element className="ms-auto flex shrink-0 items-center gap-2 whitespace-nowrap wrap-normal">{action}</Element>
      )}
    </Element>
  );
}
