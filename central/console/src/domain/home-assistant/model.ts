import {
  CONSTANTS, type EntityView, type HomeAssistantView, type LinkHealthView, type ReportRefusal, type RoomLight,
  type RoomLightsView, type RoomLightView, type Sensor,
} from "../../generated/contract";

/*
 * Home Assistant in the console's words: the link's health, which of its entities reports which
 * room light or Sensor, and the messages it could not record. An entity is Home Assistant's own
 * name ("light.bookshelf"); a room light or Sensor it reports names it by reference, source
 * CONSTANTS.HOME_ASSISTANT_SOURCE. Pure: no React, no request.
 */

const SOURCE = CONSTANTS.HOME_ASSISTANT_SOURCE;

/** The link's state in words. */
export const LINK_WORDS: Record<LinkHealthView["state"], string> = {
  "not-configured": "Not set up: Photo Wall has no Home Assistant broker address, so no light or sensor of Home "
    + "Assistant reports.",
  "never-run": "Not started: Central's worker has not run the link yet.",
  connecting: "Connecting",
  connected: "Connected",
  away: "Not connected",
};

/** The link in words, with since when for a state that has a start. */
export function linkWords(link: LinkHealthView, since: (epochSeconds: number) => string): string {
  const words = LINK_WORDS[link.state];
  return link.since !== null && link.state !== "not-configured" ? `${words} since ${since(link.since)}` : words;
}

/** Why a message was not recorded, in words: every code Central has (a code without words fails the typecheck). */
export const REFUSED_WORDS: Record<ReportRefusal, string> = {
  unknown_address: "No room light or Sensor is linked to this entity yet. Link one below, or leave it out of the "
    + "automation.",
  unreadable: "Not a message Photo Wall reads: check the automation's topic and payload.",
  out_of_range: "A value outside what a light or sensor reports: check the automation's payload.",
};

/** What an entity reports, in words: "Light: Bookshelf spot", "Sensor: Window meter", "Nothing yet". */
export function reportsWords(entity: EntityView, house: RoomLightsView): string {
  if (entity.reports !== null && entity.kind === "light") {
    const light = house.lights.find((each) => each.id === entity.reports);
    return `Light: ${light?.light.name ?? entity.reports}`;
  }
  if (entity.reports !== null) {
    const sensor = house.sensors.find((each) => each.id === entity.reports);
    return `Sensor: ${sensor?.sensor.name ?? entity.reports}`;
  }
  return "Nothing yet";
}

/** `view`'s room light, reported by `entity` from now on: its name kept, its colour kept (a colour
 * light keeps its own colour). */
export function reportedBy(view: RoomLightView, entity: string): RoomLight {
  return { kind: "reported", name: view.light.name, reporter: { source: SOURCE, address: entity }, colour: view.light.colour };
}

/** A Sensor named `name` whose readings `entity` reports. */
export function sensorAt(name: string, entity: string): Sensor {
  return { name, at: { source: SOURCE, address: entity } };
}

/** The entities in the order the page lists them: those that report nothing yet first, then by entity. */
export function entitiesInOrder(view: HomeAssistantView): EntityView[] {
  const linked = (entity: EntityView) => (entity.reports !== null ? 1 : 0);
  return [...view.entities].sort((a, b) => linked(a) - linked(b) || (a.entity < b.entity ? -1 : a.entity > b.entity ? 1 : 0));
}
