import type * as React from "react";

import { cn } from "../ui/cn";

export interface MessageProps {
  /** `status`: a live line read out when it changes; `alert`: something went wrong. */
  kind: "status" | "alert";
  children: React.ReactNode;
}

/** Message: one live line, a state's words or a problem's, with what to do about it. */
export function Message({ kind, children }: MessageProps) {
  return (
    <p role={kind} className={cn("m-0 min-w-0 text-sm wrap-anywhere", kind === "alert" ? "font-medium text-alarm" : "text-text")}>
      {children}
    </p>
  );
}
