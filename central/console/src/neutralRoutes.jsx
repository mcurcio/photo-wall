import React from "react";

import { AttentionList, attentionView } from "./AttentionList.jsx";
import { ReadinessNotice } from "./ReadinessNotice.jsx";
import { readinessRecoveryFrames } from "./readinessRecovery.js";
import SAMPLES from "./routeSamples.json";
import { formatRoute, isPlainClick } from "./routes.js";

/**
 * The Needs attention page (#/attention): the attention strip's list at full width,
 * uncapped. Each frame's entry links to `#/wall/frames/<id>/<facet>`, the facet that
 * shows its cause (health.js `facetFor`); following it asks the Wall's Inspector to
 * take focus once, as the strip's own navigation does.
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          wall: import("./wallState.js").WallMemory}} props
 */
function AttentionPage({ snapshot, central, wall }) {
  const { frameCount, rows } = attentionView(snapshot, central);
  const readinessFrames = readinessRecoveryFrames(snapshot);
  if (frameCount === 0) {
    return <p className="page__empty">No frames yet. Draw one on the Wall.</p>;
  }
  if (rows.length === 0 && readinessFrames.length === 0) {
    return <p className="page__empty">Nothing needs attention.</p>;
  }
  return <>
    {rows.length > 0 && (
      <AttentionList
        className="attention-page__list"
        rows={rows}
        entry={(row) => (
          <a
            className="attention-page__link"
            href={formatRoute(wall.frameRoute(row.frameId))}
            onClick={(event) => {
              if (isPlainClick(event)) {
                wall.prepareVisit(row.frameId);
              }
            }}
          >
            {row.text}
          </a>
        )}
      />
    )}
    {readinessFrames.length > 0 && (
      <section className="readiness-attention" aria-label="Player readiness reports">
        <h2>Player readiness reports</h2>
        <ul>
          {readinessFrames.map(({ frameId }) => (
            <li key={frameId}>
              <a
                className="attention-page__link"
                href={formatRoute(wall.frameRoute(frameId))}
                onClick={(event) => {
                  if (isPlainClick(event)) wall.prepareVisit(frameId);
                }}
              >
                {`Frame ${frameId}`}
              </a>
              <ReadinessNotice snapshot={snapshot} frameId={frameId} />
            </li>
          ))}
        </ul>
      </section>
    )}
  </>;
}

/**
 * The neutral sections (flow design §6): pages that belong to neither side. They see
 * frame health as status and link to the Wall; like the Show sections they never
 * import the Calibration facet or the Inspector (R4; tests/test_console_routes_r4.py).
 * The shell mounts a neutral section only while it is current.
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const neutralRoutes = Object.freeze(
  [
    {
      section: "attention",
      label: "Needs attention",
      render: ({ snapshot, central, wall }) => (
        <AttentionPage snapshot={snapshot} central={central} wall={wall} />
      ),
      samplePaths: SAMPLES.neutral.attention,
    },
  ].map(Object.freeze),
);
