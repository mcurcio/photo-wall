import React from "react";

import { PlayerPage } from "./PlayerPage.jsx";
import { PlayersPage } from "./PlayersPage.jsx";
import SAMPLES from "./routeSamples.json";

/**
 * The fleet sections (console DDD §9; Q1 = A, one home per box): the Players list
 * (`#/players`) and one Player page per box (`#/players/<device-id>`). Like the Wall
 * sections, the shell mounts them ONLY while current, so a Player page's node read stops
 * when the operator leaves it. Like the Show and neutral sections they never reach the
 * Commissioning facet or the Inspector (R4; tests/test_console_routes_r4.py): a Frame
 * appears here only as a link to its home on the Wall.
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const fleetRoutes = Object.freeze(
  [
    {
      section: "players",
      label: "Players",
      render: ({ snapshot, bootFacts, route, wall }) =>
        route.id === undefined ? (
          <PlayersPage snapshot={snapshot} bootFacts={bootFacts} wall={wall} />
        ) : (
          <PlayerPage key={route.id} deviceId={route.id} snapshot={snapshot} bootFacts={bootFacts} wall={wall} />
        ),
      samplePaths: SAMPLES.fleet.players,
    },
  ].map(Object.freeze),
);
