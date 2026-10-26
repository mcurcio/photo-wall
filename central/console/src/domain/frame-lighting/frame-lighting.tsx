import {
  CONSTANTS, type FrameLighting as Lighting, LIMITS, type RoomLightId, type RoomLightsView,
} from "../../generated/contract";
import { type ColourLine, ColourSwatch } from "../../patterns/colour-swatch";
import { FactGroup, FactRow, Note } from "../../patterns/fact-row";
import { OwnerLinks } from "../../patterns/link-to-owner";
import { TypedNumber } from "../../patterns/typed-number";
import { unitWords } from "../../patterns/units";
import { formatRoute } from "../../routes.js";
import { Button } from "../../ui/button";
import { Field } from "../../ui/field";
import { SegmentedControl } from "../../ui/segmented-control";
import { Select } from "../../ui/select";
import { Slider } from "../../ui/slider";
import { Inline, Stack } from "../../ui/stack";
import {
  asKind, type FrameLight, fromWords, inkWords, KIND_NAMES, type Paper, type PaperProfileChoice, referenceWords,
  sensorFor, viewing, withLight,
} from "./model";

/** The steps the editor moves by: a presentation choice inside Central's limits. */
const STEP = { lux: 1, mm: 10, whiteness: 0.01, ink: 0.1, degree: 1 } as const;
const WHITENESS_DECIMALS = 2;
const LIGHTS_MAX = LIMITS.FrameLighting.lights.maxItems;
const LUX = LIMITS.WashLight.lux;
const CLOSE = LIMITS.CloseLight;
const FAR = LIMITS.FarLight;
const PAPER = LIMITS.Paper;
const MAT = LIMITS.FrameLighting.mat_depth_mm;
const NO_PROFILE = "none";
const NO_LIGHTS = "No lights: the picture is the Display's black. Add the room lights that fall on this Frame.";
const KINDS = (Object.keys(KIND_NAMES) as FrameLight["kind"][]).map((kind) => ({ value: kind, label: KIND_NAMES[kind] }));
const KIND_HELP: Record<FrameLight["kind"], string> = {
  wash: "Even over the whole Frame, with no mat shadow: a room's general light, a spotlight's spill.",
  far: "From far away at one angle, falling on the whole Frame at once and casting the mat's shadow: a window.",
  close: "From a point near the Frame, aimed at its centre, bright inside its beam and fading to its edge: a spotlight.",
};

function LightRows({ light, number, rooms, colourOf, disabled, onChange, onRemove }: {
  light: FrameLight; number: number; rooms: RoomLightsView; colourOf: (roomLightId: RoomLightId) => ColourLine | null; disabled: boolean;
  onChange(light: FrameLight): void; onRemove(): void;
}) {
  const room = rooms.lights.find((each) => each.id === light.light);
  const sensor = sensorFor(light.light, rooms);
  const colour = room === undefined ? null : colourOf(room.id);
  const now = sensor?.reading?.kind === "measured" && sensor.reading.lux > 0 ? sensor.reading.lux : null;
  const set = (patch: Partial<FrameLight>) => onChange({ ...light, ...patch } as FrameLight);
  const options = rooms.lights.map((each) => ({ value: each.id, label: each.light.name }));
  if (room === undefined) options.push({ value: light.light, label: `${light.light} (no longer in the house)` });
  const innerMax = CLOSE.inner_beam_degrees.exclusiveMaximum - STEP.degree;
  return (
    <FactGroup title={`Light ${number}`}>
      <Stack>
        <Field label="Kind" help={KIND_HELP[light.kind]}>
          <SegmentedControl label="Kind" value={light.kind} options={KINDS} disabled={disabled}
            onChange={(kind) => onChange(asKind(light, kind))} />
        </Field>
        <Field label="Room light">
          <Select value={light.light} options={options} disabled={disabled} width="fit"
            onChange={(id) => onChange(viewing(light, id, rooms))} />
        </Field>
        {colour !== null && (
          <FactRow label="Its colour" tone={colour.tone}><ColourSwatch colour={colour.swatch}>{colour.words}</ColourSwatch></FactRow>
        )}
        <TypedNumber label="Lux at the Frame" help="What a phone light meter reads at the Frame's centre, facing out"
          value={light.lux} min={LUX.exclusiveMinimum} max={LUX.maximum} step={STEP.lux} unit="lx" exclusiveMin
          disabled={disabled} onChange={(lux) => set({ lux })} />
        {room?.light.kind === "measured" ? (
          <Inline label={`Light ${number} reference`}>
            <TypedNumber label="Its Sensor read" help="Lux the Sensor reported when you measured"
              value={light.reference.kind === "at-reading" ? light.reference.lux : 0} min={LUX.exclusiveMinimum}
              max={LUX.maximum} step={STEP.lux} unit="lx" exclusiveMin disabled={disabled}
              onChange={(lux) => set({ reference: { kind: "at-reading", lux } })} />
            <Button disabled={disabled || now === null} onClick={() => { if (now !== null) set({ reference: { kind: "at-reading", lux: now } }); }}>
              {now === null ? "Its Sensor has no reading" : `Use its reading now (${unitWords(now, "lx")})`}
            </Button>
          </Inline>
        ) : (
          <Note>{`${referenceWords(light.reference)}.`}</Note>
        )}
        {light.kind === "far" && (
          <>
            <Field label="Comes from" help="Clockwise from the wall's up: 0° above, 90° from the right">
              <Slider min={FAR.from_degrees.minimum} max={FAR.from_degrees.maximum} step={STEP.degree} unit="°"
                value={light.from_degrees} disabled={disabled} format={fromWords}
                onPreview={(next) => set({ from_degrees: next })} onCommit={(next) => set({ from_degrees: next })} />
            </Field>
            <Field label="Steepness" help="Low lights graze the wall and cast long shadows; 90° shines straight on and casts none">
              <Slider min={FAR.steep_degrees.minimum} max={FAR.steep_degrees.maximum} step={STEP.degree} unit="°"
                value={light.steep_degrees} disabled={disabled}
                onPreview={(next) => set({ steep_degrees: next })} onCommit={(next) => set({ steep_degrees: next })} />
            </Field>
            <Field label="Softness" help="How large the light looks: a bare bulb about 1°, a window about 30°">
              <Slider min={FAR.softness_degrees.minimum} max={FAR.softness_degrees.maximum} step={STEP.degree} unit="°"
                value={light.softness_degrees} disabled={disabled}
                onPreview={(next) => set({ softness_degrees: next })} onCommit={(next) => set({ softness_degrees: next })} />
            </Field>
          </>
        )}
        {light.kind === "close" && (
          <>
            <Inline label={`Light ${number} place`}>
              <TypedNumber label="Across" help="From the Frame's centre, + right" value={light.across_mm}
                min={CLOSE.across_mm.minimum} max={CLOSE.across_mm.maximum} step={STEP.mm} unit="mm" disabled={disabled}
                onChange={(across_mm) => set({ across_mm })} />
              <TypedNumber label="Up" help="+ up" value={light.up_mm}
                min={CLOSE.up_mm.minimum} max={CLOSE.up_mm.maximum} step={STEP.mm} unit="mm" disabled={disabled}
                onChange={(up_mm) => set({ up_mm })} />
              <TypedNumber label="Out" help="Out from the wall" value={light.out_mm} exclusiveMin
                min={CLOSE.out_mm.exclusiveMinimum} max={CLOSE.out_mm.maximum} step={STEP.mm} unit="mm" disabled={disabled}
                onChange={(out_mm) => set({ out_mm })} />
            </Inline>
            <Field label="Inner beam" help="Full brightness inside this angle (the bulb box's beam angle)">
              <Slider min={CLOSE.inner_beam_degrees.exclusiveMinimum + STEP.degree} max={innerMax} step={STEP.degree} unit="°"
                value={light.inner_beam_degrees} disabled={disabled}
                onPreview={(next) => set({ inner_beam_degrees: Math.min(next, light.outer_beam_degrees - STEP.degree) })}
                onCommit={(next) => set({ inner_beam_degrees: Math.min(next, light.outer_beam_degrees - STEP.degree) })} />
            </Field>
            <Field label="Outer beam" help="Fades to nothing at this angle">
              <Slider min={CLOSE.outer_beam_degrees.exclusiveMinimum + STEP.degree + STEP.degree} max={CLOSE.outer_beam_degrees.maximum}
                step={STEP.degree} unit="°" value={light.outer_beam_degrees} disabled={disabled}
                onPreview={(next) => set({ outer_beam_degrees: Math.max(next, light.inner_beam_degrees + STEP.degree) })}
                onCommit={(next) => set({ outer_beam_degrees: Math.max(next, light.inner_beam_degrees + STEP.degree) })} />
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
  paper: Paper; profiles: readonly PaperProfileChoice[] | null; disabled: boolean; onChange(paper: Paper): void;
}) {
  const listed = profiles ?? [];
  const chosen = paper.profile ?? null;
  const options = [{ value: NO_PROFILE, label: "No paper profile" },
    ...listed.map((each) => ({ value: each.profile_sha256, label: each.name })),
    ...(chosen !== null && !listed.some((each) => each.profile_sha256 === chosen.profile_sha256)
      ? [{ value: chosen.profile_sha256, label: "A paper profile no longer on the list" }] : [])];
  return (
    <FactGroup title="Paper">
      <Stack>
        <Field label="Whiteness" help={`How much light the paper's white gives back: the matching sheet is ${CONSTANTS.SHEET_WHITENESS.toFixed(WHITENESS_DECIMALS)}`}>
          <Slider min={PAPER.whiteness.minimum} max={PAPER.whiteness.maximum} step={STEP.whiteness} unit="" value={paper.whiteness}
            disabled={disabled} format={(shown) => shown.toFixed(WHITENESS_DECIMALS)}
            onPreview={(whiteness) => onChange({ ...paper, whiteness })} onCommit={(whiteness) => onChange({ ...paper, whiteness })} />
        </Field>
        <Field label="Ink density" help="How deep the ink's black goes: higher is a deeper black">
          <Slider min={PAPER.ink_density.minimum} max={PAPER.ink_density.maximum} step={STEP.ink} unit="" value={paper.ink_density}
            disabled={disabled} format={inkWords}
            onPreview={(ink_density) => onChange({ ...paper, ink_density })} onCommit={(ink_density) => onChange({ ...paper, ink_density })} />
        </Field>
        <Inline label="Ink presets">
          <Button disabled={disabled} onClick={() => onChange({ ...paper, ink_density: CONSTANTS.GLOSS_INK })}>{`Gloss (${CONSTANTS.GLOSS_INK})`}</Button>
          <Button disabled={disabled} onClick={() => onChange({ ...paper, ink_density: CONSTANTS.MATTE_INK })}>{`Matte (${CONSTANTS.MATTE_INK})`}</Button>
        </Inline>
        <Field label="Paper profile" help="Colours as this paper prints them; the only way the picture gains or loses saturation">
          <Select value={chosen?.profile_sha256 ?? NO_PROFILE} options={options} disabled={disabled} width="fit"
            onChange={(value) => {
              const next = listed.find((each) => each.profile_sha256 === value);
              onChange({ ...paper, profile: next === undefined ? (value === NO_PROFILE ? null : chosen)
                : { profile_sha256: next.profile_sha256, recipe_id: next.recipe_id } });
            }} />
        </Field>
        {listed.length === 0 && <OwnerLinks links={[{ text: "Add a paper profile in Settings › Paper profiles", href: formatRoute({ section: "paper-profiles" }) }]} />}
      </Stack>
    </FactGroup>
  );
}

export interface FrameLightingProps {
  lighting: Lighting;
  onChange(lighting: Lighting): void;
  /** The house's room lights and Sensors (null: not read yet). */
  rooms: RoomLightsView | null;
  /** The paper profiles on the list (null: not read yet). */
  profiles: readonly PaperProfileChoice[] | null;
  /** A room light's colour line (its colour and where it comes from), as the page composes it from
   * House › Lights' words; null: none to show. */
  colourOf: (roomLightId: RoomLightId) => ColourLine | null;
  disabled: boolean;
}

/**
 * The Frame's lighting: the mat's depth, up to the Frame's most lights, each a kind (Even wash, Far
 * light, Close light) of one room light with the lux a phone light meter reads at the Frame and the
 * reference it was read at, with its room light's colour line, and the paper (whiteness, ink density with the Gloss and Matte presets,
 * a paper profile). It rides the Frame's live session: every change shows on the wall, Done keeps
 * it and Revert puts it back.
 */
export function FrameLighting({ lighting, onChange, rooms, profiles, colourOf, disabled }: FrameLightingProps) {
  const full = lighting.lights.length >= LIGHTS_MAX;
  const none = rooms !== null && rooms.lights.length === 0;
  const setLight = (index: number) => (light: FrameLight) =>
    onChange({ ...lighting, lights: lighting.lights.map((each, at) => (at === index ? light : each)) });
  const removeLight = (index: number) => () =>
    onChange({ ...lighting, lights: lighting.lights.filter((_, at) => at !== index) });
  const depth = (value: number) => onChange({ ...lighting, mat_depth_mm: Math.round(value / MAT.multipleOf) * MAT.multipleOf });
  return (
    <Stack>
      <Note>
        The Pi lights the picture as these lights fall on a print in this place, live as the room&apos;s lights
        change. Measure each with a phone light meter at the Frame.
      </Note>
      <Field label="Mat depth" help="How far the mat's inner edge stands in front of the picture">
        <Slider min={MAT.minimum} max={MAT.maximum} step={MAT.multipleOf} unit="mm"
          value={lighting.mat_depth_mm} disabled={disabled} format={(shown) => unitWords(shown, "mm")} onPreview={depth} onCommit={depth} />
      </Field>
      {lighting.lights.length === 0 && <Note>{NO_LIGHTS}</Note>}
      {rooms !== null && lighting.lights.map((light, index) => (
        // A light has no identity of its own: its place in the list is it.
        <LightRows key={index} light={light} number={index + 1} rooms={rooms} colourOf={colourOf} disabled={disabled}
          onChange={setLight(index)} onRemove={removeLight(index)} />
      ))}
      {rooms === null && lighting.lights.length > 0 && <Note>Reading the house&apos;s lights…</Note>}
      <Inline>
        <Button disabled={disabled || full || rooms === null || none}
          onClick={() => { if (rooms !== null && rooms.lights.length > 0) onChange(withLight(lighting, rooms.lights[0].id, rooms)); }}>
          Add a light
        </Button>
      </Inline>
      {full && <Note>{`A Frame takes up to ${LIGHTS_MAX} lights.`}</Note>}
      {none && <OwnerLinks links={[{ text: "Add the house's lights on House › Lights first", href: formatRoute({ section: "lights" }) }]} />}
      <PaperRows paper={lighting.paper} profiles={profiles} disabled={disabled}
        onChange={(paper) => onChange({ ...lighting, paper })} />
    </Stack>
  );
}
