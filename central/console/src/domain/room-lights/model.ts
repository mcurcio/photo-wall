import {
  type ColourLineView, type ColourSensor, type ColourSensorView, CONSTANTS, type HeardEntityView, type LightColour,
  type Measured, PATTERNS, type PlayerDrawnView, type RefusedReportView, type ReportedColourSource, type ReportRefusal,
  type RoomLight, type RoomLightRefusal, type RoomLightsView, type RoomLightView, type Sensor, type SensorView,
  type Unavailable,
} from "../../generated/contract";
import type { TruthKind } from "../../design/tokens";
import type { ColourLine } from "../../patterns/colour-swatch";
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
 * "Off", "320 lx", "Not reported yet"; a light nobody reports is "On, 100 %". */
export function entryWords(entry: RoomLightEntry): string {
  if (entry.kind === "fixed") return `On, ${PERCENT} %`;
  const { state } = entry;
  switch (state.kind) {
    case "dimmer":
      return state.on ? `On, ${Math.round(state.brightness * PERCENT)} %` : "Off";
    case "measured":
      return unitWords(state.lux, "lx");
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

/** Every colour sensor's name by its id (none read yet: no names). */
export function colourSensorNames(view: RoomLightsView | null): Readonly<Record<string, string>> {
  return Object.fromEntries((view?.colour_sensors ?? []).map((each) => [each.id, each.sensor.name]));
}

/** Where a light's colour comes from, as its line names it: "its bulb", or the colour sensor's name
 * (else its id); null for a typed colour. */
function sourceWords(line: ColourLineView, names: Readonly<Record<string, string>>): string | null {
  if (line.source === "its-bulb") return "its bulb";
  if (line.source === "colour-sensor") return line.sensor === null ? "its colour sensor" : names[line.sensor] ?? line.sensor;
  return null;
}

/** The screen colour table's end a kelvin past it is drawn at, for the line's words: bounded to the
 * generated table constants only. Not the Pi's rule (player/geometry.py `drawn_kelvin` also rounds to
 * the table's step); the line names only the ends, which are on the step. Inside the table: as is. */
function tableEndKelvin(kelvin: number): number {
  return Math.min(CONSTANTS.KELVIN_TABLE_MAX, Math.max(CONSTANTS.KELVIN_TABLE_MIN, kelvin));
}

/** " · outside what the screen draws (drawn at 10,000 K)" for a kelvin outside the screen's colour
 * table, else "". */
function outsideWords(colour: LightColour): string {
  if (colour.kind !== "kelvin") return "";
  const drawn = tableEndKelvin(colour.kelvin);
  return drawn === colour.kelvin ? "" : ` · outside what the screen draws (drawn at ${unitWords(drawn, "K")})`;
}

/** A fact kept while its source can't tell: "Can't tell since 18:42 (keeping 3,400 K)" (`clock` says
 * the time); `kept` null (nothing was ever stated): "Can't tell since 18:42". */
export function keptWords(cantTellSince: number, kept: string | null, clock: (epochSeconds: number) => string): string {
  const since = `Can't tell since ${clock(cantTellSince)}`;
  return kept === null ? since : `${since} (keeping ${kept})`;
}

/**
 * A light's colour line, from Central's ColourLineView: "Typed 5,500 K"; "Following its bulb 3,400 K",
 * "Following Sky colour 3,400 K" (a colour sensor, named by `names`), "Following its bulb's colour" (a
 * bulb in a colour mode, drawn beside its swatch, `colourSwatch`); "Typed 5,500 K until its bulb says" (its source never stated one); while
 * its source can't tell, "Can't tell since 18:42 (keeping 3,400 K)", or when it never stated one,
 * "Typed 5,500 K until its bulb says · can't tell since 18:42" (a typed value is never "kept"). A kelvin outside the screen's
 * colour table adds " · outside what the screen draws (drawn at 10,000 K)". `clock` says a time.
 */
export function colourLineWords(line: ColourLineView, names: Readonly<Record<string, string>>,
  clock: (epochSeconds: number) => string): string {
  const source = sourceWords(line, names);
  const kelvin = line.colour.kind === "kelvin" ? unitWords(line.colour.kelvin, "K") : null;
  let said: string;
  if (source === null) said = `Typed ${kelvin ?? "(colour)"}`;
  else if (line.colour_from === "never-stated") {
    // Nothing stated, so nothing is kept: the typed colour stands, and can't tell is said beside it.
    said = `Typed ${kelvin ?? "(colour)"} until ${source} says`;
    if (line.cant_tell_since !== null) said += ` · can't tell since ${clock(line.cant_tell_since)}`;
  } else if (line.cant_tell_since !== null) said = keptWords(line.cant_tell_since, kelvin ?? "its colour", clock);
  else if (line.colour_from === "following") said = kelvin === null ? `Following ${source}'s colour` : `Following ${source} ${kelvin}`;
  else said = `Typed ${kelvin ?? "(colour)"}`;
  return said + outsideWords(line.colour);
}

/** The truth kind a colour line is shown in: can't tell is unknown, following is what its source
 * reported, typed (also until its source says) is what was set. */
function colourLineTone(line: ColourLineView): TruthKind {
  if (line.source !== "typed" && line.cant_tell_since !== null) return "unknown";
  return line.source !== "typed" && line.colour_from === "following" ? "reported" : "set";
}

/** A CSS rgb() channel's top: a share of 1 is this. */
const CSS_CHANNEL_MAX = 255;

/** A line's rgb colour as a CSS colour for its swatch ("rgb(255 128 0)"); null for a kelvin, which
 * its words say in full. */
export function colourSwatch(line: ColourLineView): string | null {
  if (line.colour.kind !== "rgb") return null;
  const [r, g, b] = line.colour.rgb.map((share) => Math.round(share * CSS_CHANNEL_MAX));
  return `rgb(${r} ${g} ${b})`;
}

/** A light's colour line, said where a Frame's Lighting tab shows it: its words and truth kind. */
export function colourLineSaid(line: ColourLineView, names: Readonly<Record<string, string>>,
  clock: (epochSeconds: number) => string): ColourLine {
  return { words: colourLineWords(line, names, clock), tone: colourLineTone(line), swatch: colourSwatch(line) };
}

/** A light's state now: its entry's words ("On, 60 %"), or while its reporter can't tell, the kept
 * state, "Can't tell since 18:42 (keeping On, 60 %)". */
export function brightnessWords(view: RoomLightView, clock: (epochSeconds: number) => string): string {
  const said = entryWords(view.entry);
  if (view.cant_tell_since === null) return said;
  const stated = view.entry.kind !== "fixed" && view.entry.state.kind !== "not-reported";
  return keptWords(view.cant_tell_since, stated ? said : null, clock);
}

/** A Sensor's reading now: "320 lx", or while it can't tell, "Can't tell since 18:42 (keeping 320 lx)". */
export function sensorReadingWords(view: SensorView, clock: (epochSeconds: number) => string): string {
  if (view.cant_tell_since === null) return readingWords(view.reading);
  return keptWords(view.cant_tell_since, view.reading === null ? null : readingWords(view.reading), clock);
}

/** A colour sensor's last colour: "3,400 K", "Not reported yet", or while it can't tell, "Can't tell
 * since 18:42 (keeping 3,400 K)". */
export function colourReadingWords(view: ColourSensorView, clock: (epochSeconds: number) => string): string {
  const said = view.colour === null ? null : unitWords(view.colour.kelvin, "K");
  if (view.cant_tell_since !== null) return keptWords(view.cant_tell_since, said, clock);
  return said ?? "Not reported yet";
}

/** The lights that take their colour from a colour sensor, by name ("Daylight, Hall lamp"), or "No light". */
export function followedWords(view: ColourSensorView, names: Readonly<Record<string, string>>): string {
  return view.followed_by.length === 0 ? "No light" : view.followed_by.map((id) => names[id] ?? id).join(", ");
}

/** A colour sensor named `name` whose colour smart-home colour-temperature entity `entity` reports. */
export function smartColourSensor(name: string, entity: string): ColourSensor {
  return { name, at: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: entity } };
}

/** The Pi a Sensor is wired to (its player id), or null for a Sensor another source reports. */
export function piOf(sensor: Sensor | ColourSensor): string | null {
  return sensor.at.source === CONSTANTS.PI_SOURCE ? sensor.at.address : null;
}

/** Where a Sensor is, in light terms: "Wired to Player …1a2b3c" (`piName` names the Pi), or "In the
 * smart home" for one the house's other systems report. */
export function whereWords(sensor: Sensor | ColourSensor, piName: (playerId: string) => string): string {
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
  unknown_colour_sensor: "That colour sensor is no longer there. Choose another.",
  unknown_address: "Photo Wall does not know where that report comes from.",
  reference_taken: "Another light or Sensor already reports from there. Each reports one.",
  sensor_reports_a_light: "This Sensor measures a room light. Change or remove that light first; a Sensor measures one light at most.",
  colour_sensor_followed: "A room light takes its colour from this colour sensor. Change that light's colour first.",
  named_by_a_frame: "A Frame's lighting uses this light. Remove it from those Frames' lighting first.",
  too_many: "The house has as many lights, Sensors or colour sensors as Photo Wall keeps. Remove one first.",
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

/** The smart home's entities a light, a Sensor or a colour sensor may be reported by (Home
 * Assistant's states as Central last heard them): a light from a light entity only, a Sensor from an
 * illuminance entity only, a colour sensor from a colour-temperature entity only. */
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

/** A light named `name` that reports itself through smart-home entity `entity`, its colour from `colour`. */
export function reportedThrough(name: string, entity: string, colour: ReportedColourSource): RoomLight {
  return { kind: "reported", name, reporter: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: entity }, colour };
}

/** A Sensor named `name` whose readings smart-home illuminance entity `entity` reports. */
export function smartSensor(name: string, entity: string): Sensor {
  return { name, at: { source: CONSTANTS.HOME_ASSISTANT_SOURCE, address: entity } };
}
