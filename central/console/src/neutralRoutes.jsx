import React, { useCallback } from "react";

import { readCentralErrors, readRoomLights } from "./domain/room-lights/api.ts";
import { lightNames } from "./domain/room-lights/model.ts";
import { homeCommands } from "./homeCommands.js";
import { useLibraryConnections } from "./libraryApi.js";
import { HomePage } from "./pages/home-page.tsx";
import { usePolledRead } from "./polledRead.js";
import { readinessRecoveryFrames } from "./readinessRecovery.js";
import SAMPLES from "./routeSamples.json";

// How often Home re-reads the photo library connections while it is shown.
const LIBRARY_CADENCE_MS = 15000;
// How often it re-reads the room lights Central is in doubt of: as often as House › Lights' Frames
// read the house's lights, so a doubt cleared leaves the page within it.
const SOURCES_CADENCE_MS = 5000;
const SOURCES_NOT_READ = Object.freeze({ view: null, failed: false, names: Object.freeze({}) });

/**
 * Central's errors (the room lights it is in doubt of) and the lights' names, read together; a failed
 * read keeps the last view and names and says it failed.
 */
function useSourcesInDoubt() {
  const load = useCallback(async (previous) => {
    const [errors, rooms] = await Promise.all([readCentralErrors(), readRoomLights()]);
    return Object.freeze({
      view: errors.ok ? errors.view : previous.view,
      failed: !errors.ok,
      names: rooms.ok ? lightNames(rooms.view) : previous.names,
    });
  }, []);
  return usePolledRead(load, { cadenceMs: SOURCES_CADENCE_MS, initial: SOURCES_NOT_READ }).value;
}

/**
 * Home with what it lists beside Central's own problems (decision 0036, "What Home replaces"): the
 * photo library connections that need a look, the room lights Central is in doubt of and the photo
 * app's reports, read here, and Home's writes (homeCommands.js), handed in here, so the page and
 * its domain components name neither a legacy read nor a write of another owner.
 */
function HomeWithReads({ snapshot, refresh, refreshFailed, wall, hosts, bootFacts }) {
  const library = useLibraryConnections({ cadenceMs: LIBRARY_CADENCE_MS });
  const sources = useSourcesInDoubt();
  return (
    <HomePage snapshot={snapshot} refresh={refresh} refreshFailed={refreshFailed} commands={homeCommands}
      hosts={hosts} bootFacts={bootFacts} library={library} sources={sources} lightNames={sources.names}
      readiness={readinessRecoveryFrames(snapshot)} onVisitFrame={wall.prepareVisit} />
  );
}

/**
 * The neutral section (flow design §6): Home, which belongs to neither side. It shows every Frame's
 * status and links to the Wall; like the Show sections it never imports the Frame page or its live
 * adjustment (R4; tests/test_console_routes_r4.py). The shell mounts it only while it is current.
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const neutralRoutes = Object.freeze(
  [
    {
      section: "home",
      label: "Home",
      render: ({ snapshot, refresh, refreshFailed, wall, hosts, bootFacts }) => (
        <HomeWithReads snapshot={snapshot} refresh={refresh} refreshFailed={refreshFailed} wall={wall} hosts={hosts}
          bootFacts={bootFacts} />
      ),
      samplePaths: SAMPLES.neutral.home,
    },
  ].map(Object.freeze),
);
