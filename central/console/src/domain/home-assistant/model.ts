import type { HeardEntityView, HomeAssistantStatesView, ReportRefusal, RoomLightsView } from "../../generated/contract";

/*
 * Home Assistant in the console's words: when Central last heard its states, which of its light
 * and illuminance entities reports which room light or Sensor (picked on House › Lights), and the
 * states it could not record. An entity is shown by its friendly name, its entity id
 * ("light.bookshelf") as the detail. Pure: no React, no request.
 */

/** When Central last heard Home Assistant's states: its time in words, or "Not heard yet". */
export function heardWords(view: HomeAssistantStatesView, since: (epochSeconds: number) => string): string {
  return view.heard_at === null ? "Not heard yet" : since(view.heard_at);
}

/** Why a state was not recorded, in words: every code Central has (a code without words fails the typecheck). */
export const REFUSED_WORDS: Record<ReportRefusal, string> = {
  unknown_address: "No room light or Sensor is linked to this entity yet. Pick it on House › Lights.",
  unreadable: "Not a state Photo Wall reads: check the entity in Home Assistant.",
  out_of_range: "A value outside what a light or sensor reports: check the entity in Home Assistant.",
};

/** What an entity reports, in words: "Light: Bookshelf spot", "Sensor: Window meter", "Nothing yet". */
export function reportsWords(entity: HeardEntityView, house: RoomLightsView): string {
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

/** An entity's name as the page lists it: its friendly name, else its entity id. */
export const entityName = (entity: HeardEntityView): string => entity.name ?? entity.entity;

/** The entities in the order the page lists them: those that report nothing yet first, then by name. */
export function entitiesInOrder(view: HomeAssistantStatesView): HeardEntityView[] {
  const linked = (entity: HeardEntityView) => (entity.reports !== null ? 1 : 0);
  return [...view.entities].sort((a, b) => linked(a) - linked(b) || entityName(a).localeCompare(entityName(b)));
}
