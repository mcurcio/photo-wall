import { useCallback, useEffect, useState } from "react";

import { listLuts } from "../lutsApi.js";
import { usePolledRead } from "../polledRead.js";

import { FactGroup, Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { formatRoute } from "../routes.js";
import { readRoomLights, type RoomLightView, type RoomLightsView, type SensorView } from "../roomLightsApi";
import { luxWords, stateWords } from "../roomLightWords.js";
import { Button } from "../ui/button";
import { Field, NumberInput } from "../ui/field";
import { SegmentedControl } from "../ui/segmented-control";
import { Select } from "../ui/select";
import { Slider } from "../ui/slider";
import { Inline, Stack } from "../ui/stack";

/*
 * A Frame's lighting (decision 0028; contracts/frame_lighting.py, the one home of its shape and
 * ranges): its mat depth, up to four lights, each one Frame's view of a room light, and its paper.
 * The draft is the wire's JSON as it is: liveAdjustment.js sends it whole with every trial edit.
 */

/** The room light read with it on at 100 % (contracts/frame_lighting.py DIMMER_AT_FULL). */
export const DIMMER_AT_FULL = "dimmer-at-full";
/** The lux a light was read at: at full, or the Sensor's reading at that moment. */
export type Reference = number | typeof DIMMER_AT_FULL;

interface LightBase {
  light: string;
  lux: number;
  reference: Reference;
}
export type WashLight = LightBase & { kind: "wash" };
export type FarLight = LightBase & { kind: "far"; from_degrees: number; steep_degrees: number; softness_degrees: number };
export type CloseLight = LightBase & {
  kind: "close"; across_mm: number; up_mm: number; out_mm: number; inner_beam_degrees: number; outer_beam_degrees: number;
};
export type FrameLight = WashLight | FarLight | CloseLight;

/** A paper profile (contracts/lut.py `LutRef`): the profile's digest and the recipe its LUT is baked with. */
export interface PaperProfile {
  profile_sha256: string;
  recipe_id: string;
}

export interface FrameLightingDraft {
  mat_depth_mm: number;
  lights: FrameLight[];
  paper: { whiteness: number; ink_density: number; profile: PaperProfile | null };
}

/** A paper profile the picker offers: one on the LUTs list (lutsApi.js). */
export interface ProfileChoice {
  profile_sha256: string;
  recipe_id: string;
  name: string;
}

/**
 * Every range the editor takes, as contracts/frame_lighting.py holds them (tests/
 * test_console_print_look_ranges.py holds the two equal): [min, max, step]. `gt` marks a bound
 * the contract excludes (lux and out above 0).
 */
export const RANGES = {
  mat_depth_mm: [0, 20, 0.5],
  lights: [0, 4, 1],
  lux: [0, 200000, 1],
  from_degrees: [0, 359, 1],
  steep_degrees: [5, 90, 1],
  softness_degrees: [0, 45, 1],
  across_mm: [-5000, 5000, 10],
  up_mm: [-5000, 5000, 10],
  out_mm: [0, 5000, 10],
  beam_degrees: [0, 170, 1],
  whiteness: [0.5, 1, 0.01],
  ink_density: [0.5, 5, 0.1],
} as const;
/** The ink presets (contracts/frame_lighting.py GLOSS_INK, MATTE_INK). */
export const PRESETS = { gloss: 2.3, matte: 1.6 } as const;
/** The paper as white as the matching sheet (contracts/frame_lighting.py SHEET_WHITENESS). */
export const SHEET_WHITENESS = 0.9;

const KINDS: readonly { value: FrameLight["kind"]; label: string }[] = [
  { value: "wash", label: "Even wash" },
  { value: "far", label: "Far light" },
  { value: "close", label: "Close light" },
];
const KIND_HELP: Record<FrameLight["kind"], string> = {
  wash: "Even over the whole Frame, with no mat shadow: a room's general light, a spotlight's spill.",
  far: "From far away at one angle, falling on the whole Frame at once and casting the mat's shadow: a window.",
  close: "From a point near the Frame, aimed at its centre, bright inside its beam and fading to its edge: a spotlight.",
};
const NO_PROFILE = "none";
const NO_LIGHTS = "No lights: the picture is the display's black. Add the room lights that fall on this Frame.";
const NO_ROOM_LIGHTS = "Add the house's lights in Settings › Lights first";

/** A light's fields beyond its kind's own, as they are when a light becomes that kind. */
const FAR = { from_degrees: 0, steep_degrees: 45, softness_degrees: 10 };
const CLOSE = { across_mm: 0, up_mm: 600, out_mm: 400, inner_beam_degrees: 20, outer_beam_degrees: 40 };

/** A room light's reporter as the reference reads: a Sensor's light by the Sensor's reading, every
 * other one at full. */
const measured = (room: RoomLightView | undefined) => room?.reporter.kind === "sensor";

/** The reading of the Sensor that reports `room`, when it has a number. */
function sensorReading(room: RoomLightView | undefined, sensors: readonly SensorView[]): number | null {
  if (room?.reporter.kind !== "sensor") return null;
  const sensorId = room.reporter.sensor;
  const reading = sensors.find((sensor) => sensor.id === sensorId)?.reading;
  return typeof reading === "number" && reading > 0 ? reading : null;
}

/** The reference a light gets when it starts viewing `room`: at full, or its Sensor's reading now
 * (left at full while the Sensor has none: Central then refuses it, and the editor says so). */
function referenceFor(room: RoomLightView | undefined, sensors: readonly SensorView[]): Reference {
  return sensorReading(room, sensors) ?? DIMMER_AT_FULL;
}

/** A light of `kind` keeping what it shares with `light`. */
export function asKind(light: FrameLight, kind: FrameLight["kind"]): FrameLight {
  const base = { light: light.light, lux: light.lux, reference: light.reference };
  if (kind === "far") return { kind, ...base, ...FAR, ...(light.kind === "far" ? light : {}) } as FarLight;
  if (kind === "close") return { kind, ...base, ...CLOSE, ...(light.kind === "close" ? light : {}) } as CloseLight;
  return { kind, ...base };
}

/** A light as it is added: an even wash of the first room light at 100 lx. */
export function newLight(rooms: RoomLightsView): FrameLight {
  const room = rooms.lights[0];
  return { kind: "wash", light: room.id, lux: 100, reference: referenceFor(room, rooms.sensors) };
}

/** A mat's depth, one decimal ("2.5 mm"). */
export function depthWords(depth: number): string {
  return `${depth.toFixed(1)} mm`;
}

/** Where a far light comes from, in degrees clockwise from the wall's up and as a clock face. */
export function fromWords(degrees: number): string {
  const hour = Math.round(degrees / 30) % 12;
  return `${degrees}° · ${hour === 0 ? 12 : hour} o'clock`;
}

/** A reference in words: "Read with the light at 100 %", "Read when the Sensor said 320 lx". */
export function referenceWords(reference: Reference): string {
  return reference === DIMMER_AT_FULL ? "Read with the light on at 100 %" : `Read when its Sensor said ${luxWords(reference)}`;
}

/** Ink density in words, with the preset it equals: "2.3 (Gloss)". */
export function inkWords(ink: number): string {
  const preset = ink === PRESETS.gloss ? " (Gloss)" : ink === PRESETS.matte ? " (Matte)" : "";
  return `${ink.toFixed(1)}${preset}`;
}

/** Every value of a light, in words, for a History row: its kind, room light, lux, reference and
 * its kind's own values. `names`: the room lights' names by id. */
export function lightDetails(light: FrameLight, number: number,
  names: Readonly<Record<string, string>> = {}): { label: string; value: string }[] {
  const name = `Light ${number}`;
  const kind = KINDS.find((each) => each.value === light.kind)?.label ?? light.kind;
  const rows = [
    { label: name, value: `${kind} of ${names[light.light] ?? light.light}` },
    { label: `${name} lux`, value: luxWords(light.lux) },
    { label: `${name} reference`, value: referenceWords(light.reference) },
  ];
  if (light.kind === "far") {
    rows.push({ label: `${name} comes from`, value: fromWords(light.from_degrees) },
      { label: `${name} steepness`, value: `${light.steep_degrees}°` },
      { label: `${name} softness`, value: `${light.softness_degrees}°` });
  } else if (light.kind === "close") {
    rows.push({ label: `${name} place`, value: `${light.across_mm} mm across, ${light.up_mm} mm up, ${light.out_mm} mm out` },
      { label: `${name} beam`, value: `${light.inner_beam_degrees}° inner, ${light.outer_beam_degrees}° outer` });
  }
  return rows;
}

/**
 * A number typed in a Field (a light meter's lux, a position in mm): the text as typed, applied
 * once it is a number in [min, max] (`above`: min itself excluded); otherwise the Field says so and
 * the draft keeps its value.
 */
function TypedNumber({ label, help, value, min, max, step, above = false, disabled, onChange }: {
  label: string; help?: string; value: number; min: number; max: number; step: number; above?: boolean;
  disabled: boolean; onChange: (value: number) => void;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => { setText((typed) => (Number(typed) === value ? typed : String(value))); }, [value]);
  const number = Number(text);
  const valid = text.trim() !== "" && Number.isFinite(number) && (above ? number > min : number >= min) && number <= max;
  return (
    <Field label={label} help={help}
      error={valid ? undefined : `${above ? "Above" : "From"} ${min} to ${max}`}>
      <NumberInput value={text} min={min} max={max} step={step} disabled={disabled}
        onChange={(raw) => {
          setText(raw);
          const next = Number(raw);
          if (raw.trim() !== "" && Number.isFinite(next) && (above ? next > min : next >= min) && next <= max) onChange(next);
        }} />
    </Field>
  );
}

function LightRows({ light, number, rooms, disabled, onChange, onRemove }: {
  light: FrameLight; number: number; rooms: RoomLightsView; disabled: boolean;
  onChange(light: FrameLight): void; onRemove(): void;
}) {
  const room = rooms.lights.find((each) => each.id === light.light);
  const set = (patch: Partial<FrameLight>) => onChange({ ...light, ...patch } as FrameLight);
  const now = sensorReading(room, rooms.sensors);
  const [lux, steps, degrees] = [RANGES.lux, RANGES.across_mm, RANGES.beam_degrees];
  const options = rooms.lights.map((each) => ({ value: each.id, label: each.name }));
  if (room === undefined) options.push({ value: light.light, label: `${light.light} (no longer in the house)` });
  return (
    <FactGroup title={`Light ${number}`}>
      <Stack>
        <Field label="Kind" help={KIND_HELP[light.kind]}>
          <SegmentedControl label="Kind" value={light.kind} options={KINDS} disabled={disabled}
            onChange={(kind) => onChange(asKind(light, kind))} />
        </Field>
        <Field label="Room light" notes={room === undefined ? [] : [`Now: ${stateWords(room.state)}`]}>
          <Select value={light.light} options={options} disabled={disabled} width="fit"
            onChange={(id) => {
              const next = rooms.lights.find((each) => each.id === id);
              set({ light: id, reference: referenceFor(next, rooms.sensors) });
            }} />
        </Field>
        <TypedNumber label="Lux at the Frame" help="What a phone light meter reads at the Frame's centre, facing out"
          value={light.lux} min={lux[0]} max={lux[1]} step={lux[2]} above disabled={disabled} onChange={(next) => set({ lux: next })} />
        {measured(room) ? (
          <Inline label={`Light ${number} reference`}>
            <TypedNumber label="Its Sensor read" help="Lux the Sensor reported when you measured"
              value={typeof light.reference === "number" ? light.reference : 0} min={lux[0]} max={lux[1]} step={lux[2]} above
              disabled={disabled} onChange={(next) => set({ reference: next })} />
            <Button disabled={disabled || now === null} onClick={() => { if (now !== null) set({ reference: now }); }}>
              {now === null ? "Its Sensor has no reading" : `Use its reading now (${luxWords(now)})`}
            </Button>
          </Inline>
        ) : (
          <Note>{`${referenceWords(DIMMER_AT_FULL)}.`}</Note>
        )}
        {light.kind === "far" && (
          <>
            <Field label="Comes from" help="Clockwise from the wall's up: 0° above, 90° from the right">
              <Slider min={RANGES.from_degrees[0]} max={RANGES.from_degrees[1]} step={RANGES.from_degrees[2]} unit="°" value={light.from_degrees} disabled={disabled}
                format={fromWords} onPreview={(next) => set({ from_degrees: next })} onCommit={(next) => set({ from_degrees: next })} />
            </Field>
            <Field label="Steepness" help="Low lights graze the wall and cast long shadows; 90° shines straight on and casts none">
              <Slider min={RANGES.steep_degrees[0]} max={RANGES.steep_degrees[1]} step={RANGES.steep_degrees[2]} unit="°" value={light.steep_degrees} disabled={disabled}
                onPreview={(next) => set({ steep_degrees: next })} onCommit={(next) => set({ steep_degrees: next })} />
            </Field>
            <Field label="Softness" help="How large the light looks: a bare bulb about 1°, a window about 30°">
              <Slider min={RANGES.softness_degrees[0]} max={RANGES.softness_degrees[1]} step={RANGES.softness_degrees[2]} unit="°" value={light.softness_degrees} disabled={disabled}
                onPreview={(next) => set({ softness_degrees: next })} onCommit={(next) => set({ softness_degrees: next })} />
            </Field>
          </>
        )}
        {light.kind === "close" && (
          <>
            <Inline label={`Light ${number} place`}>
              <TypedNumber label="Across (mm)" help="From the Frame's centre, + right" value={light.across_mm}
                min={steps[0]} max={steps[1]} step={steps[2]} disabled={disabled} onChange={(next) => set({ across_mm: next })} />
              <TypedNumber label="Up (mm)" help="+ up" value={light.up_mm}
                min={RANGES.up_mm[0]} max={RANGES.up_mm[1]} step={RANGES.up_mm[2]} disabled={disabled} onChange={(next) => set({ up_mm: next })} />
              <TypedNumber label="Out (mm)" help="Out from the wall" value={light.out_mm} above
                min={RANGES.out_mm[0]} max={RANGES.out_mm[1]} step={RANGES.out_mm[2]} disabled={disabled} onChange={(next) => set({ out_mm: next })} />
            </Inline>
            <Field label="Inner beam" help="Full brightness inside this angle (the bulb box's beam angle)">
              <Slider min={1} max={degrees[1] - 1} step={degrees[2]} unit="°" value={light.inner_beam_degrees} disabled={disabled}
                onPreview={(next) => set({ inner_beam_degrees: Math.min(next, light.outer_beam_degrees - 1) })}
                onCommit={(next) => set({ inner_beam_degrees: Math.min(next, light.outer_beam_degrees - 1) })} />
            </Field>
            <Field label="Outer beam" help="Fades to nothing at this angle">
              <Slider min={2} max={degrees[1]} step={degrees[2]} unit="°" value={light.outer_beam_degrees} disabled={disabled}
                onPreview={(next) => set({ outer_beam_degrees: Math.max(next, light.inner_beam_degrees + 1) })}
                onCommit={(next) => set({ outer_beam_degrees: Math.max(next, light.inner_beam_degrees + 1) })} />
            </Field>
          </>
        )}
        <Inline>
          <Button variant="ghost" disabled={disabled} onClick={onRemove}>{`Remove light ${number}`}</Button>
        </Inline>
      </Stack>
    </FactGroup>
  );
}

function PaperRows({ paper, profiles, disabled, onChange }: {
  paper: FrameLightingDraft["paper"]; profiles: readonly ProfileChoice[] | null; disabled: boolean;
  onChange(paper: FrameLightingDraft["paper"]): void;
}) {
  const listed = profiles ?? [];
  const chosen = paper.profile;
  const options = [{ value: NO_PROFILE, label: "No paper profile" },
    ...listed.map((each) => ({ value: each.profile_sha256, label: each.name })),
    ...(chosen !== null && !listed.some((each) => each.profile_sha256 === chosen.profile_sha256)
      ? [{ value: chosen.profile_sha256, label: `Paper profile ${chosen.profile_sha256.slice(0, 8)}` }] : [])];
  return (
    <FactGroup title="Paper">
      <Stack>
        <Field label="Whiteness" help="How much light the paper's white gives back: the matching sheet is 0.90">
          <Slider min={RANGES.whiteness[0]} max={RANGES.whiteness[1]} step={RANGES.whiteness[2]} unit="" value={paper.whiteness}
            disabled={disabled} format={(shown) => shown.toFixed(2)}
            onPreview={(next) => onChange({ ...paper, whiteness: next })} onCommit={(next) => onChange({ ...paper, whiteness: next })} />
        </Field>
        <Field label="Ink density" help="How deep the ink's black goes: higher is a deeper black">
          <Slider min={RANGES.ink_density[0]} max={RANGES.ink_density[1]} step={RANGES.ink_density[2]} unit="" value={paper.ink_density}
            disabled={disabled} format={inkWords}
            onPreview={(next) => onChange({ ...paper, ink_density: next })} onCommit={(next) => onChange({ ...paper, ink_density: next })} />
        </Field>
        <Inline label="Ink presets">
          <Button disabled={disabled} onClick={() => onChange({ ...paper, ink_density: PRESETS.gloss })}>{`Gloss (${PRESETS.gloss})`}</Button>
          <Button disabled={disabled} onClick={() => onChange({ ...paper, ink_density: PRESETS.matte })}>{`Matte (${PRESETS.matte})`}</Button>
        </Inline>
        <Field label="Paper profile" help="Colours as this paper prints them; the only way the picture gains or loses saturation">
          <Select value={chosen?.profile_sha256 ?? NO_PROFILE} options={options} disabled={disabled} width="fit"
            onChange={(value) => {
              const next = listed.find((each) => each.profile_sha256 === value);
              onChange({ ...paper, profile: next === undefined ? (value === NO_PROFILE ? null : chosen)
                : { profile_sha256: next.profile_sha256, recipe_id: next.recipe_id } });
            }} />
        </Field>
        {listed.length === 0 && <OwnerLinks links={[{ text: "Add a paper profile in Settings › LUTs", href: formatRoute({ section: "luts" }) }]} />}
      </Stack>
    </FactGroup>
  );
}

/** How often the room lights are read while the lighting is shown: a light's state and a Sensor's
 * reading stay current beside the light that views it. */
const ROOM_LIGHTS_CADENCE_MS = 5000;

/**
 * What the lighting editor chooses from while `open`: the house's room lights and Sensors, read every
 * ROOM_LIGHTS_CADENCE_MS (a failed read keeps the last), and the paper profiles on the LUTs list,
 * read once each time it opens. Null: not read yet.
 */
export function useLightingChoices(open: boolean): { rooms: RoomLightsView | null; profiles: ProfileChoice[] | null } {
  const load = useCallback(async (previous: RoomLightsView | null) => {
    const answer = await readRoomLights();
    return answer.ok ? answer.view : previous;
  }, []);
  const { value: rooms } = usePolledRead(load, { cadenceMs: ROOM_LIGHTS_CADENCE_MS, skip: !open, initial: null as RoomLightsView | null });
  const [profiles, setProfiles] = useState<ProfileChoice[] | null>(null);
  useEffect(() => {
    if (!open) return undefined;
    let current = true;
    void listLuts().then((result: { ok: boolean; luts?: ProfileChoice[] }) => {
      if (current && result.ok) {
        setProfiles((result.luts ?? []).map(({ profile_sha256, recipe_id, name }) => ({ profile_sha256, recipe_id, name })));
      }
    });
    return () => { current = false; };
  }, [open]);
  return { rooms, profiles };
}

export interface FrameLightingProps {
  lighting: FrameLightingDraft;
  onChange(lighting: FrameLightingDraft): void;
  /** The house's room lights and Sensors (null: not read yet). */
  rooms: RoomLightsView | null;
  /** The paper profiles on the LUTs list (null: not read yet). */
  profiles: readonly ProfileChoice[] | null;
  disabled: boolean;
}

/**
 * The Frame's lighting (decision 0028; owner, 2026-10-15: "the Frame configuration as a set of
 * physical properties and let the shader act as a sort of "physics engine""): the mat's depth, up
 * to four lights, each a kind (Even wash, Far light, Close light) of one room light with the lux a
 * phone light meter reads at the Frame and the reference it was read at, and the paper (whiteness,
 * ink density with the Gloss and Matte presets, a paper profile). Part of the Frame's Position: it
 * rides the Position tab's live session, every change shows on the wall, Done keeps it and Revert
 * puts it back.
 */
export function FrameLighting({ lighting, onChange, rooms, profiles, disabled }: FrameLightingProps) {
  const full = lighting.lights.length >= RANGES.lights[1];
  const none = rooms !== null && rooms.lights.length === 0;
  const setLight = (index: number) => (light: FrameLight) =>
    onChange({ ...lighting, lights: lighting.lights.map((each, at) => (at === index ? light : each)) });
  const removeLight = (index: number) => () =>
    onChange({ ...lighting, lights: lighting.lights.filter((_, at) => at !== index) });
  const depth = (value: number) => onChange({ ...lighting, mat_depth_mm: Math.round(value * 2) / 2 });
  return (
    <FactGroup title="Lighting">
      <Stack>
        <Note>
          The Pi lights the picture as these lights fall on a print in this place, live as the room&apos;s lights
          change. Measure each with a phone light meter at the Frame.
        </Note>
        <Field label="Mat depth" help="How far the mat's inner edge stands in front of the picture">
          <Slider min={RANGES.mat_depth_mm[0]} max={RANGES.mat_depth_mm[1]} step={RANGES.mat_depth_mm[2]} unit="mm"
            value={lighting.mat_depth_mm} disabled={disabled} format={depthWords} onPreview={depth} onCommit={depth} />
        </Field>
        {lighting.lights.length === 0 && <Note>{NO_LIGHTS}</Note>}
        {rooms !== null && lighting.lights.map((light, index) => (
          // A light has no identity of its own: its place in the list is it.
          <LightRows key={index} light={light} number={index + 1} rooms={rooms} disabled={disabled}
            onChange={setLight(index)} onRemove={removeLight(index)} />
        ))}
        {rooms === null && lighting.lights.length > 0 && <Note>Reading the house&apos;s lights…</Note>}
        <Inline>
          <Button disabled={disabled || full || rooms === null || none}
            onClick={() => { if (rooms !== null) onChange({ ...lighting, lights: [...lighting.lights, newLight(rooms)] }); }}>
            Add a light
          </Button>
        </Inline>
        {full && <Note>{`A Frame takes up to ${RANGES.lights[1]} lights.`}</Note>}
        {none && <OwnerLinks links={[{ text: NO_ROOM_LIGHTS, href: formatRoute({ section: "lights" }) }]} />}
        <PaperRows paper={lighting.paper} profiles={profiles} disabled={disabled}
          onChange={(paper) => onChange({ ...lighting, paper })} />
      </Stack>
    </FactGroup>
  );
}
