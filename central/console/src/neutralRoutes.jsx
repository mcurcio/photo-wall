import React from "react";

import { AttentionList, attentionView } from "./AttentionList.jsx";
import SAMPLES from "./routeSamples.json";
import { formatRoute } from "./routes.js";

/** A primary click with no modifier: the link opens here, not in another tab. */
function isPlainClick(event) {
  return (
    event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey
  );
}

/**
 * The Needs attention page (#/attention): the attention strip's list at full width,
 * uncapped. Each frame's entry links to `#/wall/frames/<id>/<facet>`, the facet that
 * shows its cause (health.js `facetFor`); following it asks the Wall's Inspector to
 * take focus once, as the strip's own navigation does.
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          wall: import("./WallPage.jsx").WallMemory}} props
 */
function AttentionPage({ snapshot, central, wall }) {
  const { frameCount, rows } = attentionView(snapshot, central);
  if (frameCount === 0) {
    return <p className="page__empty">No frames yet. Draw one on the Wall.</p>;
  }
  if (rows.length === 0) {
    return <p className="page__empty">Nothing needs attention.</p>;
  }
  return (
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
  );
}

/**
 * The neutral sections (flow design §6): pages that belong to neither side. They see
 * frame health as status and link to the Wall; like the Show sections they never
 * import the Commissioning facet or the Inspector (R4; tests/test_console_routes_r4.py).
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
