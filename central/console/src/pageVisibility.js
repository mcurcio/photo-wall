import { createContext, useContext } from "react";

/**
 * Whether the page holding a subtree is hidden (flow design §6). The shell keeps the Show
 * pages mounted and marks the ones not current `hidden`; each page provides this, so a
 * component that puts something in the top layer (ConfirmAction's modal `<dialog>`, which
 * `hidden` on an ancestor does not hide, and which keeps the rest of the document inert)
 * can put it away while its page is not shown. Outside any page, and on pages that mount
 * only while current, it is false.
 */
export const PageHiddenContext = createContext(false);

/** @returns {boolean} whether this subtree's page is hidden */
export function usePageHidden() {
  return useContext(PageHiddenContext);
}
