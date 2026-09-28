import React from "react";

import { wallAttention } from "./health.js";

/** "1 frame", "3 frames": the attention strip and list count frames the same way. */
export function frames(count) {
  return `${count} ${count === 1 ? "frame" : "frames"}`;
}

/**
 * @typedef {{key: string, text: string, severity?: string, frameId?: string,
 *            health?: import("./health.js").FrameHealth}} AttentionRow
 */

/**
 * What needs the operator, from the one classifier (health.js `wallAttention`): the
 * counts and one row per entry, alarms first then to-dos.
 *
 * When Central's scheduler is neither "ok" nor "disabled", the liveness alarms (health
 * `cause` "liveness") collapse into ONE causal row: Players may be unable to report
 * while it is not ok, so listing each silent frame would blame the equipment. The row
 * says "may": a stopped scheduler that holds no lock still accepts reports until the
 * last offers expire (slice 1 §8). It is the only row without a frame.
 *
 * @param {object} snapshot
 * @param {{scheduler: string|null}|null} central
 * @returns {{frameCount: number, alarms: Array<object>, todos: Array<object>,
 *            rows: AttentionRow[]}}
 */
export function attentionView(snapshot, central) {
  const { frameCount, alarms, todos } = wallAttention(snapshot);
  const stalled = central?.scheduler ?? null;
  const silenced =
    stalled === null ? [] : alarms.filter((entry) => entry.health.cause === "liveness");
  const rows = [
    ...(silenced.length > 0
      ? [
          {
            key: "scheduler",
            text:
              `${frames(silenced.length)} silent — Central's scheduler is ` +
              `${stalled.replaceAll("_", " ")}; Players may be unable to report until it recovers.`,
          },
        ]
      : []),
    ...[...alarms.filter((entry) => !silenced.includes(entry)), ...todos].map(
      ({ frame, health }) => ({
        key: frame.id,
        frameId: frame.id,
        health,
        severity: health.severity,
        text: `${frame.id} — ${health.label}`,
      }),
    ),
  ];
  return { frameCount, alarms, todos, rows };
}

/**
 * The list of frames needing attention: the attention strip's disclosure (capped) and
 * the full-width Needs attention page (#/attention) both render it.
 *
 * A row about a frame renders through `entry(row)` when given (a button on the Wall
 * side, a link on the Needs attention page); otherwise, and for the scheduler row, it
 * is plain text, so a Show page sees health as status only (R4).
 *
 * @param {{rows: AttentionRow[], cap?: number, id?: string, className?: string,
 *          entry?: ((row: AttentionRow) => React.ReactNode)|null}} props
 */
export function AttentionList({ rows, cap = Infinity, id, className, entry = null }) {
  const shown = rows.slice(0, cap);
  const more = rows.length - shown.length;
  return (
    <ul id={id} className={className} aria-label="Frames needing attention">
      {shown.map((row) => (
        <li key={row.key} className={`attention__item health--${row.severity ?? "alarm"}`}>
          {row.frameId !== undefined && entry !== null ? entry(row) : row.text}
        </li>
      ))}
      {more > 0 && <li className="attention__more">{`and ${more} more`}</li>}
    </ul>
  );
}
