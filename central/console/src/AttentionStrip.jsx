import React, { useId, useState } from "react";

import { wallAttention } from "./health.js";

// At most this many rows in the detail list; the rest are counted.
const LIST_CAP = 8;

// Liveness alarms: the frames a stalled scheduler silences (Players cannot report).
const SILENCED = new Set(["player-silent", "awaiting-report"]);

function frames(count) {
  return `${count} ${count === 1 ? "frame" : "frames"}`;
}

/**
 * The attention strip (console pass 2, slice 1 — design §5), directly under the
 * status bar: one fixed-height line saying whether any frame needs the
 * operator, with a disclosure listing which ones. The list OVERLAYS the content
 * below it, so opening it never reflows the page.
 *
 * Every state and label comes from the one classifier (health.js
 * `wallAttention`). The live region carries STATE ONLY ("2 frames need
 * attention · 3 to set up"); ages live in the list, outside it, so a screen
 * reader is not re-announced on every poll.
 *
 * When Central's scheduler is neither "ok" nor "disabled", the liveness alarms
 * collapse into ONE causal line: Players cannot report while it is stalled, so
 * listing each silent frame would blame the equipment. This is the only place
 * the strip mentions Central; the pill owns Central's health.
 *
 * In Wall mode each entry is a button calling `onNavigate(frameId)`; in
 * Showrunner mode `onNavigate` is null and entries are plain text, so the show
 * layer is never abandoned (R4). With no frames the strip renders nothing and
 * defers to the Guidance banner.
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          onNavigate: ((frameId: string) => void)|null}} props
 */
export function AttentionStrip({ snapshot, central, onNavigate }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const { frameCount, alarms, todos } = wallAttention(snapshot);
  if (frameCount === 0) {
    return null;
  }

  const summary =
    alarms.length === 0 && todos.length === 0
      ? `All ${frames(frameCount)} heard from`
      : [
          alarms.length > 0 &&
            `${frames(alarms.length)} ${alarms.length === 1 ? "needs" : "need"} attention`,
          todos.length > 0 && `${todos.length} to set up`,
        ]
          .filter(Boolean)
          .join(" · ");

  const stalled = central?.scheduler ?? null;
  const silenced = stalled === null ? [] : alarms.filter((entry) => SILENCED.has(entry.health.state));
  const rows = [
    ...(silenced.length > 0
      ? [
          {
            key: "scheduler",
            text:
              `${frames(silenced.length)} silent — Central's scheduler is ` +
              `${stalled.replaceAll("_", " ")}; Players cannot report until it recovers.`,
          },
        ]
      : []),
    ...[...alarms.filter((entry) => !silenced.includes(entry)), ...todos].map(
      ({ frame, health }) => ({
        key: frame.id,
        frameId: frame.id,
        severity: health.severity,
        text: `${frame.id} — ${health.label}`,
      }),
    ),
  ];
  const shown = rows.slice(0, LIST_CAP);
  const more = rows.length - shown.length;
  const severity = alarms.length > 0 ? "alarm" : todos.length > 0 ? "todo" : "ok";

  return (
    <section className="attention" aria-label="Wall attention">
      <div className="attention__line">
        <span className={`attention__summary health--${severity}`} role="status">
          {summary}
        </span>
        {rows.length > 0 && (
          <button
            type="button"
            className="attention__toggle"
            aria-expanded={open}
            aria-controls={listId}
            onClick={() => setOpen((current) => !current)}
          >
            {open ? "Hide frames" : "Show frames"}
          </button>
        )}
      </div>
      {open && rows.length > 0 && (
        <ul id={listId} className="attention__list" aria-label="Frames needing attention">
          {shown.map((row) => (
            <li key={row.key} className={`attention__item health--${row.severity ?? "alarm"}`}>
              {row.frameId !== undefined && onNavigate !== null ? (
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
              ) : (
                row.text
              )}
            </li>
          ))}
          {more > 0 && <li className="attention__more">{`and ${more} more`}</li>}
        </ul>
      )}
    </section>
  );
}
