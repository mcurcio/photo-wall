import React from "react";

import { AttentionPage } from "./AttentionPage.jsx";
import { useLibraryConnections } from "./libraryApi.js";
import SAMPLES from "./routeSamples.json";

// How often Needs attention re-reads the photo library connections while it is shown.
const LIBRARY_CADENCE_MS = 15000;

/**
 * Needs attention with the photo library connections it lists (roadmap 4, K1): the read lives
 * here, beside the page, so the page itself reaches no write module (G1).
 */
function AttentionWithLibrary({ snapshot, central, wall, hosts, bootFacts }) {
  const library = useLibraryConnections({ cadenceMs: LIBRARY_CADENCE_MS });
  return (
    <AttentionPage snapshot={snapshot} central={central} wall={wall} hosts={hosts} bootFacts={bootFacts}
      library={library} />
  );
}

/**
 * The neutral sections (flow design §6): pages that belong to neither side. They see
 * frame health as status and link to the Wall; like the Show sections they never
 * import the Frame page or its live adjustment (R4; tests/test_console_routes_r4.py).
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
        <AttentionWithLibrary snapshot={snapshot} central={central} wall={wall} hosts={hosts} bootFacts={bootFacts} />
      ),
      samplePaths: SAMPLES.neutral.attention,
    },
  ].map(Object.freeze),
);
