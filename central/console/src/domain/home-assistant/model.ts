import type {
  GalleriaView, HeardEntityView, HomeAssistantStatesView, ReportRefusal, RoomLightsView,
} from "../../generated/contract";

/*
 * Home Assistant in the console's words: when Central last heard its states, Galleria's version
 * beside Central's, which of its light, illuminance and colour-temperature entities reports which
 * room light, Sensor or colour sensor (picked on House › Lights), and the states it could not
 * record. An entity is shown by its friendly name, its entity id
 * ("light.bookshelf") as the detail. Pure: no React, no request.
 */

/** When Central last heard Home Assistant's states: its time in words, or "Not heard yet". */
export function heardWords(view: HomeAssistantStatesView, since: (epochSeconds: number) => string): string {
  return view.heard_at === null ? "Not heard yet" : since(view.heard_at);
}

/** Why a state was not recorded, in words: every code Central has (a code without words fails the typecheck). */
export const REFUSED_WORDS: Record<ReportRefusal, string> = {
  unknown_address: "No room light, Sensor or colour sensor is linked to this entity yet. Pick it on House › Lights.",
  unreadable: "Not a state Photo Wall reads: check the entity in Home Assistant.",
  out_of_range: "A value outside what a light or sensor reports: check the entity in Home Assistant.",
};

/** What an entity reports, in words: "Light: Bookshelf spot", "Sensor: Window meter", "Colour sensor:
 * Sky colour", "Nothing yet". */
export function reportsWords(entity: HeardEntityView, house: RoomLightsView): string {
  if (entity.reports === null) return "Nothing yet";
  switch (entity.kind) {
    case "light":
      return `Light: ${house.lights.find((each) => each.id === entity.reports)?.light.name ?? entity.reports}`;
    case "illuminance":
      return `Sensor: ${house.sensors.find((each) => each.id === entity.reports)?.sensor.name ?? entity.reports}`;
    default:
      return `Colour sensor: ${house.colour_sensors.find((each) => each.id === entity.reports)?.sensor.name ?? entity.reports}`;
  }
}

/** Galleria's version beside Central's: "Galleria v0.40.0 · Central 0.40.0" ("Central 0.40.0" alone
 * while Galleria has not said its version). */
export function versionWords(galleria: GalleriaView): string {
  const central = `Central ${galleria.central_version}`;
  return galleria.version === null ? central : `Galleria ${galleria.version} · ${central}`;
}

/** What to do about Galleria, or null when nothing: Central judges `update`, the words are the console's. */
export function updateWords(galleria: GalleriaView): string | null {
  if (galleria.version === null) return "Galleria has not said its version: update it in HACS";
  return galleria.update ? "Update Galleria in HACS: this version cannot send colour sensors yet" : null;
}

/** An entity's name as the page lists it: its friendly name, else its entity id. */
export const entityName = (entity: HeardEntityView): string => entity.name ?? entity.entity;

/** The entities in the order the page lists them: those that report nothing yet first, then by name. */
export function entitiesInOrder(view: HomeAssistantStatesView): HeardEntityView[] {
  const linked = (entity: HeardEntityView) => (entity.reports !== null ? 1 : 0);
  return [...view.entities].sort((a, b) => linked(a) - linked(b) || entityName(a).localeCompare(entityName(b)));
}
