import React from "react";

import SAMPLES from "./routeSamples.json";
import { WallPage } from "./WallPage.jsx";

/**
 * The Wall section (flow design §6): the plan and each Frame's page, with its Position and
 * Picture tabs. It is the only route that reaches Display controls (R4), and the shell mounts
 * it ONLY while it is current, so no hidden Show page ever holds a live adjustment. The
 * Players (the boxes) have their own home in the fleet table (fleetRoutes.jsx).
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const wallRoutes = Object.freeze(
  [
    {
      section: "wall",
      label: "Wall",
      render: ({ snapshot, bootFacts, route, navigate, wall, hosts }) => (
        <WallPage
          snapshot={snapshot}
          bootFacts={bootFacts}
          hosts={hosts}
          route={route}
          navigate={navigate}
          memory={wall}
        />
      ),
      samplePaths: SAMPLES.wall.wall,
      // A Frame's page names the Frame in its own heading (pages/frame-page.tsx).
      ownsHeading: (route) => route.id !== undefined,
    },
  ].map(Object.freeze),
);
