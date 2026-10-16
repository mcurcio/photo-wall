import {
  CONSTANTS, type FrameLighting, type FrameLightingRefusal, type RoomLight, type RoomLightId, type RoomLightsView,
  type SensorView,
} from "../../generated/contract";
import { unitWords } from "../../patterns/units";

/*
 * A Frame's lighting in the console's terms: its lights (each one Frame's view of a room light, in
 * lux at the Frame with the reference it was read at), its mat and its paper. Shapes and limits are
 * Central's (src/generated/contract.ts); Central checks every draft against the house and refuses one
 * that does not fit. Pure: no React, no request.
 */

/** One Frame light: an even wash, a far light or a close light. */
export type FrameLight = FrameLighting["lights"][number];
/** The lux a Frame light was read at: with its room light at full, or while its Sensor read a lux. */
export type Reference = FrameLight["reference"];
export type Paper = FrameLighting["paper"];
export type PaperProfileRef = NonNullable<Paper["profile"]>;

/** A paper profile the Frame's paper may name: one on the paper profiles list, with its name. */
export interface PaperProfileChoice extends PaperProfileRef {
  name: string;
}

/** The lux a new light starts at, at the Frame. */
export const NEW_LUX = 100;
/** A light's fields beyond its kind's own, as they are when a light becomes that kind. */
const FAR = { from_degrees: 0, steep_degrees: 45, softness_degrees: 10 } as const;
const CLOSE = { across_mm: 0, up_mm: 600, out_mm: 400, inner_beam_degrees: 20, outer_beam_degrees: 40 } as const;
/** Hours on a clock face, and the degrees each is. */
const HOURS = 12;
const DEGREES_AN_HOUR = 30;
const AT_FULL: Reference = { kind: "at-full" };

export const KIND_NAMES: Record<FrameLight["kind"], string> = { wash: "Even wash", far: "Far light", close: "Close light" };

/** The Sensor's reading in lux, when it has one above 0 (a reference must be above 0). */
function readingOf(sensor: SensorView | undefined): number | null {
  const reading = sensor?.reading;
  return reading?.kind === "measured" && reading.lux > 0 ? reading.lux : null;
}

/** The reference a Frame light of `light` is read at: for a light a Sensor measures, the Sensor's
 * reading now (AtReading); for any other, at full (AtFull). A measured light whose Sensor has no
 * reading yet gets AtFull, which Central refuses until a reading is typed. */
export function referenceFor(light: RoomLight, sensor: SensorView | undefined): Reference {
  const lux = light.kind === "measured" ? readingOf(sensor) : null;
  return lux === null ? AT_FULL : { kind: "at-reading", lux };
}

/** The Sensor that measures room light `id`, if any. */
export function sensorFor(id: RoomLightId, rooms: RoomLightsView): SensorView | undefined {
  const light = rooms.lights.find((each) => each.id === id)?.light;
  return light?.kind === "measured" ? rooms.sensors.find((sensor) => sensor.id === light.sensor) : undefined;
}

/** `light` viewing room light `id` instead, with the reference that room light takes. */
export function viewing(light: FrameLight, id: RoomLightId, rooms: RoomLightsView): FrameLight {
  const room = rooms.lights.find((each) => each.id === id);
  return { ...light, light: id, reference: room === undefined ? AT_FULL : referenceFor(room.light, sensorFor(id, rooms)) };
}

/** `lighting` with one more light: an even wash of room light `roomLightId` at NEW_LUX, with the
 * reference that room light takes. */
export function withLight(lighting: FrameLighting, roomLightId: RoomLightId, rooms: RoomLightsView): FrameLighting {
  const added = viewing({ kind: "wash", light: roomLightId, lux: NEW_LUX, reference: AT_FULL }, roomLightId, rooms);
  return { ...lighting, lights: [...lighting.lights, added] };
}

/** A light of `kind` keeping what it shares with `light`. */
export function asKind(light: FrameLight, kind: FrameLight["kind"]): FrameLight {
  const base = { light: light.light, lux: light.lux, reference: light.reference };
  if (kind === "far") return { ...FAR, ...(light.kind === "far" ? light : {}), ...base, kind };
  if (kind === "close") return { ...CLOSE, ...(light.kind === "close" ? light : {}), ...base, kind };
  return { ...base, kind };
}

/** A reference in words: "Read with the light on at 100 %", "Read when its Sensor said 320 lx". */
export function referenceWords(reference: Reference): string {
  return reference.kind === "at-full" ? "Read with the light on at 100 %" : `Read when its Sensor said ${unitWords(reference.lux, "lx")}`;
}

/** Where a far light comes from, in degrees clockwise from the wall's up and as a clock face. */
export function fromWords(degrees: number): string {
  const hour = Math.round(degrees / DEGREES_AN_HOUR) % HOURS;
  return `${unitWords(degrees, "°")} · ${hour === 0 ? HOURS : hour} o'clock`;
}

/** Ink density in words, with the preset it equals: "2.3 (Gloss)". */
export function inkWords(ink: number): string {
  const preset = ink === CONSTANTS.GLOSS_INK ? " (Gloss)" : ink === CONSTANTS.MATTE_INK ? " (Matte)" : "";
  return `${ink.toFixed(1)}${preset}`;
}

/** A paper profile's name from the list, else "A paper profile no longer on the list". */
export function profileWords(profile: PaperProfileRef | null | undefined, profiles: readonly PaperProfileChoice[]): string {
  if (profile === null || profile === undefined) return "None";
  return profiles.find((each) => each.profile_sha256 === profile.profile_sha256)?.name ?? "A paper profile no longer on the list";
}

/** Every value of a light, in words, for a History row: its kind, room light, lux, reference and
 * its kind's own values. `names`: the room lights' names by id. */
export function lightDetails(light: FrameLight, number: number,
  names: Readonly<Record<string, string>> = {}): { label: string; value: string }[] {
  const name = `Light ${number}`;
  const rows = [
    { label: name, value: `${KIND_NAMES[light.kind]} of ${names[light.light] ?? light.light}` },
    { label: `${name} lux`, value: unitWords(light.lux, "lx") },
    { label: `${name} reference`, value: referenceWords(light.reference) },
  ];
  if (light.kind === "far") {
    rows.push({ label: `${name} comes from`, value: fromWords(light.from_degrees) },
      { label: `${name} steepness`, value: unitWords(light.steep_degrees, "°") },
      { label: `${name} softness`, value: unitWords(light.softness_degrees, "°") });
  } else if (light.kind === "close") {
    rows.push({ label: `${name} place`, value: `${unitWords(light.across_mm, "mm")} across, ${unitWords(light.up_mm, "mm")} up, `
      + `${unitWords(light.out_mm, "mm")} out` },
    { label: `${name} beam`, value: `${unitWords(light.inner_beam_degrees, "°")} inner, ${unitWords(light.outer_beam_degrees, "°")} outer` });
  }
  return rows;
}

/** Each refusal of a Frame's lighting, in words: every code Central has (a code without words fails the typecheck). */
export const LIGHTING_REFUSAL_WORDS: Record<FrameLightingRefusal, string> = {
  unknown_room_light: "A light names a room light that is no longer in the house. Choose another, or add it on House › Lights.",
  reference_mismatch: "A light's reference does not match how its room light is known: a light a Sensor measures needs "
    + "the Sensor's reading, any other light is read with it on at 100 %. Choose its room light again.",
  unknown_paper_profile: "The paper names a paper profile that is no longer on the list. Choose another.",
  lighting_too_large: "This lighting is more than a Frame takes. Remove a light.",
};
