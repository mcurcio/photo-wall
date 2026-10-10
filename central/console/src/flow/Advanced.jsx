import React, { useId } from "react";

import { ChevronIcon } from "../icons.jsx";

/**
 * The values of a step that have a stated default (flow design §3 rule 1, §4;
 * presentational): a WAI-ARIA disclosure. The button is named "Advanced", carries
 * `aria-expanded` and `aria-controls`, and is described by `summary` (the values
 * inside, such as "Loop: On"), which stays visible while the panel is closed. The
 * panel uses the `hidden` attribute, so closed values leave the accessibility tree;
 * Review lists them all the same.
 *
 * The caller owns `open`, so a flow can open it to route a problem to a field inside.
 * `lockedOpen` holds it open while a value inside must be answered (a name with no
 * usable id): the toggle then says it is expanded and is disabled.
 *
 * @param {{summary: React.ReactNode, open: boolean, lockedOpen?: boolean, onToggle: () => void,
 *          children: React.ReactNode}} props
 */
export function Advanced({ summary, open, lockedOpen = false, onToggle, children }) {
  const shown = open || lockedOpen;
  const id = useId();
  return (
    <div className={`advanced${shown ? " advanced--open" : ""}`}>
      <div className="advanced__head">
        <button
          type="button"
          className="advanced__toggle"
          aria-expanded={shown}
          aria-controls={`${id}-panel`}
          aria-describedby={`${id}-summary`}
          disabled={lockedOpen}
          onClick={onToggle}
        >
          <ChevronIcon />
          Advanced
        </button>
        <span id={`${id}-summary`} className="advanced__summary">
          {summary}
        </span>
      </div>
      <div id={`${id}-panel`} className="advanced__panel" hidden={!shown}>
        {children}
      </div>
    </div>
  );
}
