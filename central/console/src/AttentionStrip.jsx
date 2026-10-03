import React, { useId, useRef, useState } from "react";

import { AttentionList, attentionSummary, attentionView } from "./AttentionList.jsx";
import { formatRoute, isPlainClick } from "./routes.js";

// At most this many rows in the detail list; the rest are counted.
const LIST_CAP = 8;

/**
 * The attention strip (console pass 2, slice 1 — design §5), directly under the
 * status bar: one fixed-height line saying whether any frame needs the
 * operator, with a disclosure listing which Frames and Players. The list OVERLAYS the content
 * below it, so opening it never reflows the page.
 *
 * Every state and label comes from the one classifier (health.js
 * `wallAttention`) and the one host classifier (hostHealth.js `hostIncidents`). It
 * counts incidents only (AttentionList.jsx `attentionSummary`): structural to-dos are
 * the Wall's To finish list (console DDD §61). The live region carries STATE ONLY
 * ("2 Frames · 1 Player need attention"); ages live in the list, outside it, so a screen reader is not
 * re-announced on every poll.
 *
 * The rows, including the one causal line that replaces the liveness alarms
 * while Central's scheduler is not ok, come from AttentionList.jsx
 * (`attentionView`), which the Needs attention page (#/attention) shares. This
 * is the only place the strip mentions Central; the pill owns Central's health.
 *
 * The disclosure closes on Escape and returns focus to its toggle. Below the
 * list, "Show all" opens the Needs attention page; a plain click on it calls
 * `onShowAll()`, with which the shell focuses that page's heading (the link itself
 * leaves with the closing list).
 *
 * On the Wall side, the Players pages and the Needs attention page each entry is
 * a button calling `onNavigate(frameId)`; on a Show page `onNavigate` is null
 * and entries are plain text, so the show layer is never abandoned (R4). With
 * no frames the strip renders nothing and defers to the Guidance banner.
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          hosts?: import("./fleetHosts.js").FleetHosts|null,
 *          bootFacts?: object|null,
 *          onNavigate: ((frameId: string) => void)|null,
 *          onShowAll?: () => void}} props
 */
export function AttentionStrip({ snapshot, central, hosts = null, bootFacts = null, onNavigate, onShowAll }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const toggleRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const view = attentionView(snapshot, central, hosts, bootFacts);
  const { frameCount, severity, rows } = view;
  if (frameCount === 0) {
    return null;
  }

  const summary = attentionSummary(view);

  return (
    <section
      className="attention"
      aria-label="Wall attention"
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.preventDefault();
          setOpen(false);
          toggleRef.current?.focus();
        }
      }}
    >
      <div className="attention__line">
        <span className={`attention__summary health--${severity}`} role="status">
          {summary}
        </span>
        {rows.length > 0 && (
          <button
            ref={toggleRef}
            type="button"
            className="console__button attention__toggle"
            aria-expanded={open}
            aria-controls={listId}
            onClick={() => setOpen((current) => !current)}
          >
            {open ? "Hide list" : "Show list"}
          </button>
        )}
      </div>
      {open && rows.length > 0 && (
        <div className="attention__panel">
          <AttentionList
            id={listId}
            className="attention__list"
            rows={rows}
            cap={LIST_CAP}
            entry={
              onNavigate === null
                ? null
                : (row) => (
                    <button
                      type="button"
                      className="attention__entry"
                      onClick={() => {
                        setOpen(false);
                        onNavigate(row.frameId);
                      }}
                    >
                      {row.text}
                    </button>
                  )
            }
          />
          <a
            className="attention__all"
            href={formatRoute({ section: "attention" })}
            onClick={(event) => {
              if (isPlainClick(event)) {
                setOpen(false);
                onShowAll?.();
              }
            }}
          >
            Show all
          </a>
        </div>
      )}
    </section>
  );
}
