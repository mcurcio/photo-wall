import React from "react";

import { PlayerPage } from "./PlayerPage.jsx";
import { PlayersPage } from "./PlayersPage.jsx";
import { ReleasesPage } from "./ReleasesPage.jsx";
import SAMPLES from "./routeSamples.json";
import { UpdateWallPage } from "./UpdateWallPage.jsx";

/**
 * The fleet sections (console DDD §9; Q1 = A, one home per box): the Players list
 * (`#/players`) and one Player page per box (`#/players/<device-id>`); and Releases
 * (`#/releases`, Part E §25), the home of the fleet-wide release aggregates, with its Update the
 * wall journey (`#/releases/update/<tag>[/try/<player-id>]`, §25a). Like the Wall
 * sections, the shell mounts them ONLY while current, so a Player page's node read and the
 * release read stop when the operator leaves them. Like the Show and neutral sections they never reach the
 * Calibration facet or the Inspector (R4; tests/test_console_routes_r4.py): a Frame
 * appears here only as a link to its home on the Wall.
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const fleetRoutes = Object.freeze(
  [
    {
      section: "players",
      label: "Players",
      render: ({ snapshot, bootFacts, route, wall, hosts }) =>
        route.id === undefined ? (
          <PlayersPage snapshot={snapshot} bootFacts={bootFacts} wall={wall} hosts={hosts} />
        ) : (
          <PlayerPage key={route.id} deviceId={route.id} snapshot={snapshot} bootFacts={bootFacts} wall={wall}
            hosts={hosts} />
        ),
      samplePaths: SAMPLES.fleet.players,
    },
    {
      section: "releases",
      label: "Releases",
      render: ({ snapshot, bootFacts, route, navigate }) =>
        route.flow === "update" ? (
          <UpdateWallPage key={route.id} tag={route.id} tried={route.tried ?? null}
            skipped={route.skipped ?? []} snapshot={snapshot}
            bootFacts={bootFacts} navigate={navigate} />
        ) : (
          <ReleasesPage />
        ),
      samplePaths: SAMPLES.fleet.releases,
    },
  ].map(Object.freeze),
);
