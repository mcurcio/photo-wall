import React from "react";

import { EquipmentRoster } from "./EquipmentRoster.jsx";
import { PlayerVersions } from "./PlayerVersions.jsx";
import SAMPLES from "./routeSamples.json";
import { WallPage } from "./WallPage.jsx";

/**
 * The Wall sections (flow design §6): the plan, the Inspector with its Commissioning
 * facet, and the equipment. These are the only routes that reach Display controls
 * (R4), and the shell mounts each of them ONLY while it is current, so no hidden Show
 * page ever holds Commissioning DOM.
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
    {
      section: "equipment",
      label: "Equipment",
      render: ({ snapshot, bootFacts, wall }) => (
        <>
          <PlayerVersions snapshot={snapshot} />
          <EquipmentRoster snapshot={snapshot} bootFacts={bootFacts} onNavigate={wall.visitFrame} />
        </>
      ),
      samplePaths: SAMPLES.wall.equipment,
    },
  ].map(Object.freeze),
);
