import {
  CONSTANTS, type FixedColour, type HeardEntityView, type Measured, PATTERNS, type PlayerDrawnView, type RefusedReportView,
  type ReportedColour, type ReportRefusal, type RoomLight, type RoomLightRefusal, type RoomLightsView,
  type RoomLightView, type Sensor, type SensorView, type Unavailable,
} from "../../generated/contract";
import { unitWords } from "../../patterns/units";

/*
 * Room lights in the console's words: the house's real lights, the Sensors that read them, and
 * their last states, in light terms only. Who reports a light is said as the light's own fact
 * ("Reports itself", "Measured by Window meter", "Always on"); which integration and which of its
 * names sit behind it is the integration's page's. Pure: no React, no request. Shapes and limits
 * are Central's (src/generated/contract.ts).
 */

/** A room light's entry in the room-lights document: its colour and state, one kind per reporter. */
export type RoomLightEntry = RoomLightView["entry"];

/** A share as a whole percent. */
const PERCENT = 100;
/** A new light's colour temperature: a warm bulb. */
export const NEW_KELVIN = 2700;
const ID_FORM = new RegExp(PATTERNS.RoomLightId);

/** A light's last state, said from its entry alone (never from when it was heard): "On, 60 %",
 * "Off", "320 lx", "Unavailable", "Not reported yet"; a light nobody reports is "On, 100 %". */
export function entryWords(entry: RoomLightEntry): string {
  if (entry.kind === "fixed") return `On, ${PERCENT} %`;
  const { state } = entry;
  switch (state.kind) {
    case "dimmer":
    case "colour-dimmer":
      return state.on ? `On, ${Math.round(state.brightness * PERCENT)} %` : "Off";
    case "measured":
      return unitWords(state.lux, "lx");
    case "unavailable":
      return "Unavailable";
    default:
      return "Not reported yet";
  }
}

/** A Sensor's last reading: "320 lx", "Unavailable", "Not reported yet" (null: never heard). */
export function readingWords(reading: Measured | Unavailable | null): string {
  if (reading === null) return "Not reported yet";
  return reading.kind === "measured" ? unitWords(reading.lux, "lx") : "Unavailable";
}

/** Who reports a light, in light terms: "Reports itself", "Measured by Window meter" (the Sensor's
 * name, else its id), "Always on". */
export function reporterWords(light: RoomLight, sensors: readonly SensorView[]): string {
  switch (light.kind) {
    case "reported":
      return "Reports itself";
    case "measured":
      return `Measured by ${sensors.find((sensor) => sensor.id === light.sensor)?.sensor.name ?? light.sensor}`;
    default:
      return "Always on";
  }
}

/** Every room light's name by its id (none read yet: no names). */
export function lightNames(view: RoomLightsView | null): Readonly<Record<string, string>> {
  return Object.fromEntries((view?.lights ?? []).map((light) => [light.id, light.light.name]));
}

/** A light's colour: "2700 K", or "Its own colour (red 50 %, green 30 %, blue 20 %)". */
export function colourWords(colour: FixedColour | ReportedColour): string {
  if (colour.kind === "fixed") return unitWords(colour.kelvin, "K");
  const [red, green, blue] = colour.channel_shares.map((share) => Math.round(share * PERCENT));
  return `Its own colour (red ${red} %, green ${green} %, blue ${blue} %)`;
}

/** The Pi a Sensor is wired to (its player id), or null for a Sensor another source reports. */
export function piOf(sensor: Sensor): string | null {
  return sensor.at.source === CONSTANTS.PI_SOURCE ? sensor.at.address : null;
}

/** Where a Sensor is, in light terms: "Wired to Player …1a2b3c" (`piName` names the Pi), or "In the
 * smart home" for one the house's other systems report. */
export function whereWords(sensor: Sensor, piName: (playerId: string) => string): string {
  const pi = piOf(sensor);
  return pi === null ? "In the smart home" : `Wired to ${piName(pi)}`;
}

/** The Sensor wired to `playerId`, if any. */
export function sensorOfPi(sensors: readonly SensorView[], playerId: string): SensorView | undefined {
  return sensors.find((sensor) => piOf(sensor.sensor) === playerId);
}

/** A Sensor named `name`, wired to Pi `playerId`: the one place the console builds a Pi's reference. */
export function piSensor(name: string, playerId: string): Sensor {
  return { name, at: { source: CONSTANTS.PI_SOURCE, address: playerId } };
}

/** The one refusal log's entries about Pi `playerId`'s Sensor, newest first as Central lists them. */
export function refusedOfPi(refused: readonly RefusedReportView[], playerId: string): RefusedReportView[] {
  return refused.filter((each) => each.source === CONSTANTS.PI_SOURCE && each.address === playerId);
}

/** Why a Pi's reading was not recorded, in words: every code Central has (a code without words fails
 * the typecheck). */
export const REFUSED_READING_WORDS: Record<ReportRefusal, string> = {
  unknown_address: "Photo Wall does not know this Pi.",
  unreadable: "Not a reading Photo Wall reads: the Sensor's driver may be wrong.",
  out_of_range: "A reading outside what a light Sensor measures: the Sensor may be faulty.",
};

/** The id a new light or Sensor gets from its name: lower case, each run of other characters one
 * hyphen, shortened until it is an id ("Bookshelf spot" is "bookshelf-spot"); "" for a name with
 * no letter or digit. */
export function idFromName(name: string): string {
  let id = name.normalize("NFKD").replace(/\p{Mn}/gu, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+/, "").replace(/-+$/, "");
  while (id !== "" && !ID_FORM.test(id)) id = id.slice(0, -1).replace(/-+$/, "");
  return id;
}

/** The id a new item named `name` gets, or what is wrong with the name. */
export function newId(name: string, taken: readonly string[]): { id: string } | { problem: string } {
  const id = idFromName(name);
  if (!ID_FORM.test(id)) return { problem: "Give it a name with a letter or a digit." };
  if (taken.includes(id)) return { problem: `Another one already has the id ${id}: choose another name.` };
  return { id };
}

/** When a light was last heard: Central's time in words (`since`), "Always on" for a light nobody
 * reports, "Never" before its reporter's first word. */
export function heardWords(view: RoomLightView, since: (epochSeconds: number) => string): string {
  if (view.heard_at !== null) return since(view.heard_at);
  return view.light.kind === "fixed" ? "Always on" : "Never";
}

/** Each refusal of a light or Sensor change, in words: every code Central has (a code without words
 * fails the typecheck). */
export const REFUSAL_WORDS: Record<RoomLightRefusal, string> = {
  unknown_room_light: "That light is no longer there.",
  unknown_sensor: "That Sensor is no longer there. Choose another.",
  unknown_address: "Photo Wall does not know where that report comes from.",
  reference_taken: "Another light or Sensor already reports from there. Each reports one.",
  sensor_reports_a_light: "This Sensor measures a room light. Change or remove that light first; a Sensor measures one light at most.",
  named_by_a_frame: "A Frame's lighting uses this light. Remove it from those Frames' lighting first.",
  too_many: "The house has as many lights or Sensors as Photo Wall keeps. Remove one first.",
};
const UNREACHABLE = "Central did not answer. Try again.";
const INVALID = "Photo Wall refused these values. Check the name and the numbers.";

/** A refusal in words, with what did not happen first ("Not removed: …"). */
export function refusalWords(done: string, code: string): string {
  const words = code in REFUSAL_WORDS ? REFUSAL_WORDS[code as RoomLightRefusal]
    : code === "unreachable" ? UNREACHABLE : code === "http_422" ? INVALID : `Central refused it (${code}).`;
  return `${done}: ${words}`;
}

/** Whether a Pi draws with the house's lights as they are now, in words. */
export const DRAWN_WORDS: Record<PlayerDrawnView["state"], string> = {
  drawn: "Up to date: it draws with the house's lights as they are now.",
  behind: "Behind: it still draws with an older copy of the house's lights.",
  "not-following": "Not following: it cannot read the house's lights, so it keeps the last ones it had.",
  "not-reported": "Not reported: it has not said which of the house's lights it draws with.",
};

/** The drawn state Central judged for `playerId` (none: Central has no word of it). */
export function drawnOf(players: readonly PlayerDrawnView[], playerId: string): PlayerDrawnView | undefined {
  return players.find((player) => player.player_id === playerId);
}

/** The smart home's entities a light or a Sensor may be reported by (Home Assistant's states as
 * Central last heard them): a light from a light entity only, a Sensor from an illuminance entity only. */
export function entitiesFor<K extends HeardEntityView["kind"]>(heard: readonly HeardEntityView[], kind: K):
  Extract<HeardEntityView, { kind: K }>[] {
  return heard.filter((entity): entity is Extract<HeardEntityView, { kind: K }> => entity.kind === kind);
}

/** An entity as the picker shows it: its name, with its entity id as the detail ("Bookshelf (light.bookshelf)"). */
export function entityLabel(entity: HeardEntityView): string {
  return entity.name === null ? entity.entity : `${entity.name} (${entity.entity})`;
}

/** The smart-home entity a light reports itself through, or null for a light that does not. */
export function reporterOf(light: RoomLight | null): string | null {
  return light?.kind === "reported" && light.reporter.source === CONSTANTS.HOME_ASSISTANT_SOURCE ? light.reporter.address : null;
}

/** A light named `name` that reports itself through smart-home entity `entity`, in `colour`. */
export function reportedThrough(name: string, entity: string, colour: FixedColour | ReportedColour): RoomLight {
  return { kind: "reported", name, reporter: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: entity }, colour };
}

/** A Sensor named `name` whose readings smart-home illuminance entity `entity` reports. */
export function smartSensor(name: string, entity: string): Sensor {
  return { name, at: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: entity } };
}
