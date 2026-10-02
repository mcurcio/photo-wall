import React from "react";

import SAMPLES from "./routeSamples.json";
import { WallPage } from "./WallPage.jsx";

/**
 * The Wall section (flow design §6): the plan and the Inspector with its Commissioning
 * facet. It is the only route that reaches Display controls (R4), and the shell mounts it
 * ONLY while it is current, so no hidden Show page ever holds Commissioning DOM. The
 * Players (the boxes) have their own home in the fleet table (fleetRoutes.jsx).
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const wallRoutes = Object.freeze(
  [
    {
      section: "wall",
      label: "Wall",
      render: ({ snapshot, bootFacts, route, navigate, wall, recovery }) => (
        <WallPage
          snapshot={snapshot}
          bootFacts={bootFacts}
          route={route}
          navigate={navigate}
          memory={wall}
          recovery={recovery}
        />
      ),
      samplePaths: SAMPLES.wall.wall,
    },
  ].map(Object.freeze),
);
