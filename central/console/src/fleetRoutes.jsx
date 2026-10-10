import React from "react";

import { HardwarePage } from "./pages/hardware-page.tsx";
import { HardwarePiPage } from "./pages/hardware-pi-page.tsx";
import { PlayerPage } from "./PlayerPage.jsx";
import { ReleasesPage } from "./ReleasesPage.jsx";
import SAMPLES from "./routeSamples.json";
import { UpdateWallPage } from "./UpdateWallPage.jsx";

/**
 * The fleet sections (console by domain § Fleet: two projections of one Pi): Hardware
 * (`#/hardware`, focused by `?pi=<device-id>`, and one Pi's Hardware page
 * `#/hardware/<device-id>`); one Pi's Software and screens page (`#/players/<device-id>`,
 * reached from the Pi header only, so it has no sidebar link and no list until the Software
 * list lands); and Releases (`#/releases`, Part E §25), the home of the fleet-wide release
 * aggregates, with its Update the wall journey (`#/releases/update/<tag>[/try/<player-id>]`,
 * §25a). Like the Wall sections, the shell mounts them ONLY while current, so a Pi page's node
 * read and the release read stop when the operator leaves them. Like the Show and neutral
 * sections they never reach the Frame page or its live adjustment (R4;
 * tests/test_console_routes_r4.py): a Frame appears here only as a link to its home on the Wall.
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const fleetRoutes = Object.freeze(
  [
    {
      section: "hardware",
      label: "Hardware",
      render: ({ snapshot, bootFacts, route, hosts }) =>
        route.id === undefined ? (
          <HardwarePage snapshot={snapshot} bootFacts={bootFacts} hosts={hosts} focus={route.pi ?? null} />
        ) : (
          <HardwarePiPage key={route.id} deviceId={route.id} snapshot={snapshot} bootFacts={bootFacts}
            hosts={hosts} />
        ),
      samplePaths: SAMPLES.fleet.hardware,
    },
    {
      section: "players",
      label: "Software and screens",
      inSidebar: false,
      render: ({ snapshot, bootFacts, route, wall }) => (
        <PlayerPage key={route.id} deviceId={route.id} snapshot={snapshot} bootFacts={bootFacts} wall={wall} />
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
