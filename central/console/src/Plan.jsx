import React from "react";

import { project } from "./projection.js";

/**
 * Read-only per-Surface plan (Bead 1 / tracer).
 *
 * Renders the selected Surface's placed frames as hand-coded SVG `<rect>`
 * elements from their committed `x_mm/y_mm/width_mm/height_mm`, scaled to fit the
 * viewport by the pure `project()` helper. Each frame is selectable — clicking
 * (or Enter/Space on the focused frame) calls `onSelect(frameId)`; `selection` is
 * the currently-selected `frameId|null`. Every frame carries an accessible name
 * that embeds its frame id, so tests locate frames by identity via role/text —
 * never by coordinates (design §1c, tracer testing philosophy).
 *
 * Origin-stacked / geometry-less frames are NOT drawn here; they belong to the
 * Unplaced tray (see UnplacedTray.jsx).
 *
 * @param {{snapshot: object|null, surfaceId: string|null,
 *          selection: string|null, onSelect: (frameId: string) => void}} props
 */
const VIEWPORT = { width: 960, height: 600 };

export function Plan({ snapshot, surfaceId, selection, onSelect }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const { placed } = project(frames, surfaceId, VIEWPORT);

  return (
    <section
      className="plan"
      role="group"
      aria-label={`Wall plan for surface ${surfaceId}`}
    >
      <svg
        className="plan__svg"
        viewBox={`0 0 ${VIEWPORT.width} ${VIEWPORT.height}`}
        width={VIEWPORT.width}
        height={VIEWPORT.height}
        preserveAspectRatio="xMinYMin meet"
      >
        {placed.map(({ id, rect }) => {
          const selected = selection === id;
          return (
            <g
              key={id}
              className="plan__frame"
              role="button"
              tabIndex={0}
              aria-label={`Frame ${id}`}
              aria-pressed={selected}
              onClick={() => onSelect(id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect(id);
                }
              }}
            >
              <rect
                x={rect.x}
                y={rect.y}
                width={rect.w}
                height={rect.h}
                className={selected ? "plan__rect plan__rect--selected" : "plan__rect"}
              />
              <text
                x={rect.x + rect.w / 2}
                y={rect.y + rect.h / 2}
                className="plan__label"
                textAnchor="middle"
                dominantBaseline="middle"
              >
                {id}
              </text>
            </g>
          );
        })}
      </svg>
    </section>
  );
}
