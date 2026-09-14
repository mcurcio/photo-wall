import React from "react";

import { connectivity, nowShowing } from "./join.js";
import { project } from "./projection.js";

/**
 * Read-only per-Surface plan (Bead 1 / tracer; Bead 2 adds now-showing chips).
 *
 * Renders the selected Surface's placed frames as hand-coded SVG `<rect>`
 * elements from their committed `x_mm/y_mm/width_mm/height_mm`, scaled to fit the
 * viewport by the pure `project()` helper. Each frame is selectable — clicking
 * (or Enter/Space on the focused frame) calls `onSelect(frameId)`; `selection` is
 * the currently-selected `frameId|null`. Every frame carries an accessible name
 * that embeds its frame id, so tests locate frames by identity via role/text —
 * never by coordinates (design §1c, tracer testing philosophy).
 *
 * Alongside each drawn frame the plan renders a status readout (Bead 2): the
 * intended now-showing chip "Scheduled: <scene_id>" + phase (from `nowShowing`),
 * a connectivity dot (from `connectivity`), and a `calibration_valid` badge. The
 * chip asserts operator INTENT, never confirmed playback — the word "LIVE" is
 * deliberately absent (design §6a). The status lives in its OWN `<g role="group">`
 * (aria-label "Frame <id> status") rather than inside the selectable button group,
 * so its text and connectivity `img` stay independently discoverable and are not
 * folded into the button's accessible name.
 *
 * Origin-stacked / geometry-less frames are NOT drawn here; they belong to the
 * Unplaced tray (see UnplacedTray.jsx).
 *
 * @param {{snapshot: object|null, surfaceId: string|null,
 *          selection: string|null, onSelect: (frameId: string) => void}} props
 */
const VIEWPORT = { width: 960, height: 600 };

const CONNECTIVITY_LABEL = {
  connected: "Player connected",
  disconnected: "Player disconnected",
  unbound: "Player unbound",
};

export function Plan({ snapshot, surfaceId, selection, onSelect }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const framesById = new Map(frames.map((frame) => [frame.id, frame]));
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
          const now = nowShowing(snapshot?.runtime, id);
          const connected = connectivity(snapshot, id);
          const frame = framesById.get(id);
          const calibrationValid = frame?.calibration_valid === true;
          return (
            <React.Fragment key={id}>
              <g
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
              <g className="plan__status" role="group" aria-label={`Frame ${id} status`}>
                <circle
                  cx={rect.x + 10}
                  cy={rect.y + 12}
                  r={5}
                  className={`plan__dot plan__dot--${connected}`}
                  role="img"
                  aria-label={CONNECTIVITY_LABEL[connected]}
                />
                {now === null ? (
                  <text x={rect.x + 22} y={rect.y + 16} className="plan__chip plan__chip--idle">
                    Not scheduled
                  </text>
                ) : (
                  <>
                    <text x={rect.x + 22} y={rect.y + 16} className="plan__chip">
                      {`Scheduled: ${now.scene_id}`}
                    </text>
                    <text x={rect.x + 22} y={rect.y + 30} className="plan__phase">
                      {`Phase: ${now.phase}`}
                    </text>
                  </>
                )}
                <text
                  x={rect.x + 22}
                  y={rect.y + 44}
                  className={`plan__badge plan__badge--${calibrationValid ? "valid" : "invalid"}`}
                >
                  {calibrationValid ? "Calibration valid" : "Calibration invalid"}
                </text>
              </g>
            </React.Fragment>
          );
        })}
      </svg>
    </section>
  );
}
