import React from "react";

import { FrameHealthBadges } from "./FrameHealthBadges.jsx";
import { MediaPipeline } from "./MediaPipeline.jsx";
import { ProgramsRegion } from "./ProgramsRegion.jsx";
import SAMPLES from "./routeSamples.json";
import { RunsRegion } from "./RunsRegion.jsx";
import { SceneFlow } from "./SceneFlow.jsx";
import { SourcesRegion } from "./SourcesRegion.jsx";

/**
 * The Show sections (flow design §6): what is showing and the library behind it.
 *
 * The shell keeps every Show section MOUNTED and hides the ones not current with the
 * HTML `hidden` attribute (rule 2), so a draft in any of them survives a section
 * change, a Wall visit, a poll and a session expiry.
 *
 * R4: this module, and everything it imports, never reaches the Commissioning facet
 * or the Inspector that hosts it; frame health appears only as status badges.
 * tests/test_console_routes_r4.py walks the imports to prove it, and a browser test
 * visits every `samplePaths` entry (routeSamples.json, `show`).
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const showRoutes = Object.freeze(
  [
    {
      section: "now",
      label: "Now showing",
      render: ({ snapshot }) => (
        <>
          <FrameHealthBadges snapshot={snapshot} />
          <RunsRegion snapshot={snapshot} />
          <MediaPipeline snapshot={snapshot} />
        </>
      ),
      samplePaths: SAMPLES.show.now,
    },
    {
      section: "scenes",
      label: "Scenes",
      render: ({ snapshot, route, navigate, rememberScene, markDraft }) => (
        <section className="showrunner__region" role="region" aria-label="Scenes">
          <h2 className="showrunner__region-title">Scenes</h2>
          <SceneFlow
            snapshot={snapshot}
            route={route}
            navigate={navigate}
            rememberScene={rememberScene}
            markDraft={markDraft}
          />
        </section>
      ),
      samplePaths: SAMPLES.show.scenes,
    },
    {
      section: "schedule",
      label: "Schedule",
      render: ({ snapshot }) => <ProgramsRegion snapshot={snapshot} />,
      samplePaths: SAMPLES.show.schedule,
    },
    {
      section: "sources",
      label: "Photo sources",
      render: ({ snapshot }) => <SourcesRegion snapshot={snapshot} />,
      samplePaths: SAMPLES.show.sources,
    },
  ].map(Object.freeze),
);
