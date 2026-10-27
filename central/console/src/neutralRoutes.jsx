import React, { useCallback } from "react";

import { AttentionPage } from "./AttentionPage.jsx";
import { readCentralErrors, readRoomLights } from "./domain/room-lights/api.ts";
import { lightNames } from "./domain/room-lights/model.ts";
import { useLibraryConnections } from "./libraryApi.js";
import { usePolledRead } from "./polledRead.js";
import SAMPLES from "./routeSamples.json";

// How often Needs attention re-reads the photo library connections while it is shown.
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
 * Needs attention with the photo library connections and the room lights in doubt it lists (roadmap
 * 4, K1; Central's errors): the reads live here, beside the page, so the page itself reaches no write
 * module (G1).
 */
function AttentionWithLibrary({ snapshot, central, wall, hosts, bootFacts }) {
  const library = useLibraryConnections({ cadenceMs: LIBRARY_CADENCE_MS });
  const sources = useSourcesInDoubt();
  return (
    <AttentionPage snapshot={snapshot} central={central} wall={wall} hosts={hosts} bootFacts={bootFacts}
      library={library} sources={sources} lightNames={sources.names} />
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
