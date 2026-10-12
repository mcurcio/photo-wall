import { useState } from "react";
import type * as React from "react";

import { Sheet } from "../ui/sheet";
import { Split } from "../ui/split";

export interface EditorPageProps {
  /** The page's EntityHeader: its one title. */
  header: React.ReactNode;
  /** The settings, in their fixed order of concern. */
  form: React.ReactNode;
  /** The preview's title, and its region's accessible name ("Photos in this Source"). */
  previewTitle: string;
  /** The preview; drawn beside the form, or in a Sheet on a phone. */
  preview: () => React.ReactNode;
  /** On a phone, the one line the preview collapses to ("2,301 photos · 14 videos"). */
  previewLine: string;
  /** The SaveBar. */
  saveBar: React.ReactNode;
}

/**
 * EditorPage (design language T4, the Editor template): the form and its live preview side by
 * side from `lg`, the preview sticky so a change shows beside the setting that made it; under
 * the form between `md` and `lg`; and on a phone a count line that opens the preview in a Sheet.
 * The SaveBar stays at the bottom of the view.
 */
export function EditorPage({ header, form, previewTitle, preview, previewLine, saveBar }: EditorPageProps) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {header}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={
          "flex min-h-11 w-full cursor-pointer items-center rounded-card border border-line "
          + "bg-surface-raised px-4 py-2 text-left font-sans text-sm text-text md:hidden "
          + "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        }
      >
        <Split as="span" action={<span className="text-accent" aria-hidden="true">Show ›</span>}>{previewLine}</Split>
      </button>
      <div className="grid min-w-0 gap-6 lg:grid-cols-2 lg:items-start">
        <div className="flex min-w-0 flex-col gap-3">{form}</div>
        <section
          aria-label={previewTitle}
          className={
            "hidden min-w-0 rounded-card border border-line bg-surface-raised px-4 py-3 md:block "
            + "lg:sticky lg:top-4 lg:max-h-screen lg:overflow-y-auto"
          }
        >
          <h3 className="m-0 mb-2 text-base font-medium text-text">{previewTitle}</h3>
          {preview()}
        </section>
      </div>
      {saveBar}
      <Sheet open={open} onOpenChange={setOpen} title={previewTitle}>
        <section aria-label={previewTitle}>{open ? preview() : null}</section>
      </Sheet>
    </div>
  );
}
