import React from "react";

import { AttentionPage } from "./AttentionPage.jsx";
import SAMPLES from "./routeSamples.json";

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
      render: ({ snapshot, central, wall, hosts, bootFacts }) => (
        <AttentionPage snapshot={snapshot} central={central} wall={wall} hosts={hosts} bootFacts={bootFacts} />
      ),
      samplePaths: SAMPLES.neutral.attention,
    },
  ].map(Object.freeze),
);
