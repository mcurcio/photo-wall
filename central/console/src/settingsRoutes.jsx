import React from "react";

import { HomeAssistantPage } from "./pages/home-assistant-page.tsx";
import { HouseLightsPage } from "./pages/house-lights-page.tsx";
import { HouseSettingsPage } from "./pages/house-settings-page.tsx";
import { PaperProfilesPage } from "./pages/paper-profiles-page.tsx";
import { PeopleAndAccessPage } from "./pages/people-and-access-page.tsx";
import { PhotoFitSettingsPage } from "./pages/photo-fit-settings-page.tsx";
import { PhotoLibraryPage } from "./pages/photo-library-page.tsx";
import SAMPLES from "./routeSamples.json";
import { useSnapshot } from "./useSnapshot.js";

/**
 * People and access with the session's Log out: the page is the catalog's and reaches no session
 * state of its own, so the one owner's `signOut` is handed to it here.
 */
function PeopleAndAccessWithSession() {
  const { signOut } = useSnapshot();
  return <PeopleAndAccessPage onLogOut={() => signOut()} />;
}

/**
 * The Settings sections (console design §4, "Set up › Settings"; design-language T8): house-wide
 * configuration, one area per section. Roadmap 4 adds the first, Photo library
 * (`#/photo-library`): the Immich connections, pasted and checked in the console; the photo
 * follow-ups add Photo fit (`#/photo-fit`): the fit rule's tuning values; Power and the week adds
 * House (`#/house`): the house's timezone, location, units and clock; room light adds House › Lights
 * (`#/lights`): the house's room lights and Sensors, and Home Assistant (`#/home-assistant`): its
 * link, which entity reports which light or Sensor, and the messages it could not record; the
 * print look adds Paper profiles (`#/paper-profiles`): the ICC profiles a Frame's paper is made from;
 * operator sign-in adds People and access (`#/people-and-access`): who is signed in and the
 * deployment's identity provider, read-only (decision 0035). Like the fleet
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
    {
      section: "people-and-access",
      label: "People and access",
      render: () => <PeopleAndAccessWithSession />,
      samplePaths: SAMPLES.settings["people-and-access"],
    },
  ].map(Object.freeze),
);
