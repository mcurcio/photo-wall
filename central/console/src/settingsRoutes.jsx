import React from "react";

import { PhotoLibraryPage } from "./pages/photo-library-page.tsx";
import SAMPLES from "./routeSamples.json";

/**
 * The Settings sections (console design §4, "Set up › Settings"; design-language T8): house-wide
 * configuration, one area per section. Roadmap 4 adds the first, Photo library
 * (`#/photo-library`): the Immich connections, pasted and checked in the console. Like the fleet
 * and neutral sections, the shell mounts a Settings section only while it is current, so its
 * read stops when the operator leaves it; it never reaches the Frame page (R4).
 *
 * @type {ReadonlyArray<import("./Shell.jsx").RouteEntry>}
 */
export const settingsRoutes = Object.freeze(
  [
    {
      section: "photo-library",
      label: "Photo library",
      render: () => <PhotoLibraryPage />,
      samplePaths: SAMPLES.settings["photo-library"],
    },
  ].map(Object.freeze),
);
