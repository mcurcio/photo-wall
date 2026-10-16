import React from "react";

import { HomeAssistantPage } from "./pages/home-assistant-page.tsx";
import { HouseLightsPage } from "./pages/house-lights-page.tsx";
import { HouseSettingsPage } from "./pages/house-settings-page.tsx";
import { PaperProfilesPage } from "./pages/paper-profiles-page.tsx";
import { PhotoFitSettingsPage } from "./pages/photo-fit-settings-page.tsx";
import { PhotoLibraryPage } from "./pages/photo-library-page.tsx";
import SAMPLES from "./routeSamples.json";

/**
 * The Settings sections (console design §4, "Set up › Settings"; design-language T8): house-wide
 * configuration, one area per section. Roadmap 4 adds the first, Photo library
 * (`#/photo-library`): the Immich connections, pasted and checked in the console; the photo
 * follow-ups add Photo fit (`#/photo-fit`): the fit rule's tuning values; Power and the week adds
 * House (`#/house`): the house's timezone, location, units and clock; room light adds House › Lights
 * (`#/lights`): the house's room lights and Sensors, and Home Assistant (`#/home-assistant`): its
 * link, which entity reports which light or Sensor, and the messages it could not record; the
 * print look adds Paper profiles (`#/paper-profiles`): the ICC profiles a Frame's paper is made from. Like the fleet
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
    {
      section: "photo-fit",
      label: "Photo fit",
      render: ({ snapshot }) => <PhotoFitSettingsPage snapshot={snapshot} />,
      samplePaths: SAMPLES.settings["photo-fit"],
    },
    {
      section: "house",
      label: "House",
      render: () => <HouseSettingsPage />,
      samplePaths: SAMPLES.settings.house,
    },
    {
      section: "lights",
      label: "House › Lights",
      render: ({ snapshot }) => <HouseLightsPage snapshot={snapshot} />,
      samplePaths: SAMPLES.settings.lights,
    },
    {
      section: "home-assistant",
      label: "Home Assistant",
      render: () => <HomeAssistantPage />,
      samplePaths: SAMPLES.settings["home-assistant"],
    },
    {
      section: "paper-profiles",
      label: "Paper profiles",
      render: () => <PaperProfilesPage />,
      samplePaths: SAMPLES.settings["paper-profiles"],
    },
  ].map(Object.freeze),
);
