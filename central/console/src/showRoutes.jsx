import React from "react";

import { FrameHealthBadges } from "./FrameHealthBadges.jsx";
import { MediaPipeline } from "./MediaPipeline.jsx";
import { ProgramsRegion } from "./ProgramsRegion.jsx";
import SAMPLES from "./routeSamples.json";
import { RunsRegion } from "./RunsRegion.jsx";
import { SceneFlow } from "./SceneFlow.jsx";
import { SHOW_KEYS } from "./showNowModel.js";
import { SourcesRegion } from "./SourcesRegion.jsx";

/**
 * The Show sections (flow design §6): what is showing and the library behind it.
 *
 * The shell keeps every Show section MOUNTED and hides the ones not current with the
 * HTML `hidden` attribute (rule 2), so a draft in any of them survives a section
 * change, a Wall visit, a poll and a session expiry.
 *
 * R4: this module, and everything it imports, never reaches the Position tab
 * or the Frame page that hosts it; frame health appears only as status badges.
 * tests/test_console_routes_r4.py walks the imports to prove it, and a browser test
 * visits every `samplePaths` entry (routeSamples.json, `show`).
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const showRoutes = Object.freeze(
  [
    {
      section: "now",
      label: "Now",
      // The Show-now flow (#/now/show/<step>) lives in the Runs region; while it shows a
      // step, the media pipeline is left out (rule 1: one question on screen).
      render: ({ snapshot, route, navigate, recentScene, markDraft }) => (
        <>
          <FrameHealthBadges snapshot={snapshot} />
          <RunsRegion
            snapshot={snapshot}
            route={route}
            navigate={navigate}
            recentScene={recentScene}
            markDraft={markDraft}
          />
          {SHOW_KEYS.fromRoute(route) === null && <MediaPipeline snapshot={snapshot} />}
        </>
      ),
      samplePaths: SAMPLES.show.now,
    },
    {
      section: "scenes",
      label: "Scenes",
      render: ({ snapshot, route, navigate, rememberScene, markDraft, handOffs }) => (
        <section className="showrunner__region" role="region" aria-label="Scenes">
          <h2 className="showrunner__region-title">Scenes</h2>
          <SceneFlow
            snapshot={snapshot}
            route={route}
            navigate={navigate}
            rememberScene={rememberScene}
            markDraft={markDraft}
            handOffs={handOffs}
          />
        </section>
      ),
      samplePaths: SAMPLES.show.scenes,
    },
    {
      section: "schedule",
      label: "Schedule",
      render: ({ snapshot, route, navigate, recentScene, markDraft }) => (
        <ProgramsRegion
          snapshot={snapshot}
          route={route}
          navigate={navigate}
          recentScene={recentScene}
          markDraft={markDraft}
        />
      ),
      samplePaths: SAMPLES.show.schedule,
    },
    {
      section: "sources",
      label: "Sources",
      render: ({ snapshot, route, navigate, markDraft, handOffs }) => (
        <SourcesRegion
          snapshot={snapshot}
          route={route}
          navigate={navigate}
          markDraft={markDraft}
          handOffs={handOffs}
        />
      ),
      samplePaths: SAMPLES.show.sources,
    },
  ].map(Object.freeze),
);
