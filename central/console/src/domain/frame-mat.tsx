import { FactGroup, Note } from "../patterns/fact-row";
import { Button } from "../ui/button";
import { Field } from "../ui/field";
import { Slider } from "../ui/slider";
import { Inline, Stack } from "../ui/stack";

/** One light the room shines on the Frame (contracts/models.py `Light`): where it comes from,
 * clockwise from the wall's up, how steeply, how soft its shadow's edge, how strong against the
 * room's general light (in tenths) and its colour. */
export interface LightDraft {
  from_degrees: number;
  steep_degrees: number;
  softness_degrees: number;
  strength_tenths: number;
  colour_kelvin: number;
}

/** The Frame's mat (contracts/models.py `Mat`): how far its inner edge stands in front of the
 * picture, and up to four lights. */
export interface MatDraft {
  depth_mm: number;
  lights: LightDraft[];
}

export interface FrameMatProps {
  mat: MatDraft;
  onChange(mat: MatDraft): void;
  disabled: boolean;
}

/** The most lights a mat takes (contracts/models.py MAT_LIGHTS_MAX). */
export const LIGHTS_MAX = 4;
/** A light as it is added: from the wall's up, steep 45 degrees, softness 10, strength 1.0, 6500 K. */
export const NEW_LIGHT: LightDraft = {
  from_degrees: 0, steep_degrees: 45, softness_degrees: 10, strength_tenths: 10, colour_kelvin: 6500,
};

/** Where a light comes from, in degrees clockwise from the wall's up and as a clock face
 * ("90° · 3 o'clock"). */
export function fromWords(degrees: number): string {
  const hour = Math.round(degrees / 30) % 12;
  return `${degrees}° · ${hour === 0 ? 12 : hour} o'clock`;
}

/** An angle in whole degrees ("30°"). */
export function degreesWords(degrees: number): string {
  return `${degrees}°`;
}

/** A light's strength in tenths, as the number it reads ("1.0"). */
export function strengthWords(tenths: number): string {
  return (tenths / 10).toFixed(1);
}

/** A mat's depth in millimetres, one decimal ("2.5 mm"). */
export function depthWords(depth: number): string {
  return `${depth.toFixed(1)} mm`;
}

function LightRows({ light, number, disabled, onChange, onRemove }: {
  light: LightDraft; number: number; disabled: boolean; onChange(light: LightDraft): void; onRemove(): void;
}) {
  const set = (field: keyof LightDraft) => (value: number) => onChange({ ...light, [field]: value });
  const tenths = (value: number) => onChange({ ...light, strength_tenths: Math.round(value * 10) });
  return (
    <FactGroup title={`Light ${number}`}>
      <Stack>
        <Field label="Comes from" help="Clockwise from the wall's up: 0° above, 90° from the right">
          <Slider min={0} max={359} step={1} unit="°" value={light.from_degrees} disabled={disabled}
            format={fromWords} onPreview={set("from_degrees")} onCommit={set("from_degrees")} />
        </Field>
        <Field label="Steepness" help="Low lights graze the wall and cast long shadows; 90° shines straight on and casts none">
          <Slider min={5} max={90} step={1} unit="°" value={light.steep_degrees} disabled={disabled}
            format={degreesWords} onPreview={set("steep_degrees")} onCommit={set("steep_degrees")} />
        </Field>
        <Field label="Softness" help="How large the light looks: a bare bulb about 1°, a window about 30°">
          <Slider min={0} max={45} step={1} unit="°" value={light.softness_degrees} disabled={disabled}
            format={degreesWords} onPreview={set("softness_degrees")} onCommit={set("softness_degrees")} />
        </Field>
        <Field label="Strength" help="Against the room's other light: 1.0 is as bright as all the rest">
          <Slider min={0} max={10} step={0.1} unit="" value={light.strength_tenths / 10} disabled={disabled}
            format={(shown) => strengthWords(Math.round(shown * 10))} onPreview={tenths} onCommit={tenths} />
        </Field>
        <Field label="Colour" help="6500 K is the display's white; lower is warmer, higher cooler">
          <Slider min={2000} max={10000} step={100} unit="K" value={light.colour_kelvin} disabled={disabled}
            onPreview={set("colour_kelvin")} onCommit={set("colour_kelvin")} />
        </Field>
        <Inline>
          <Button variant="ghost" disabled={disabled} onClick={onRemove}>{`Remove light ${number}`}</Button>
        </Inline>
      </Stack>
    </FactGroup>
  );
}

/**
 * The Frame's mat and the lights that fall on it (print look, design R5, R6; owner, 2026-10-12:
 * "cool white light coming from the side AND warm white coming from the top"): the mat's depth,
 * 0 to 20 mm in 0.5, and up to four lights, each with where it comes from, its steepness,
 * softness, strength and colour. The Pi draws their shadows inside the opening; they never recolour
 * the picture. No lights or no depth: no shadow. Part of the Frame's Position, so it rides the
 * Position tab's live session, Done and Revert.
 */
export function FrameMat({ mat, onChange, disabled }: FrameMatProps) {
  const full = mat.lights.length >= LIGHTS_MAX;
  const setLight = (index: number) => (light: LightDraft) =>
    onChange({ ...mat, lights: mat.lights.map((each, at) => (at === index ? light : each)) });
  const removeLight = (index: number) => () =>
    onChange({ ...mat, lights: mat.lights.filter((_, at) => at !== index) });
  const depth = (value: number) => onChange({ ...mat, depth_mm: Math.round(value * 2) / 2 });
  const shadowless = mat.lights.length === 0 ? "No lights: no shadow." : mat.depth_mm === 0 ? "Depth 0.0 mm: no shadow." : null;
  return (
    <FactGroup title="Mat">
      <Stack>
        <Note>The Pi draws the shadows these lights cast from the mat onto the picture. They never change its colours.</Note>
        <Field label="Depth" help="How far the mat's inner edge stands in front of the picture">
          <Slider min={0} max={20} step={0.5} unit="mm" value={mat.depth_mm} disabled={disabled}
            format={depthWords} onPreview={depth} onCommit={depth} />
        </Field>
        {shadowless !== null && <Note>{shadowless}</Note>}
        {mat.lights.map((light, index) => (
          // A light has no identity of its own: its place in the list is it.
          <LightRows key={index} light={light} number={index + 1} disabled={disabled}
            onChange={setLight(index)} onRemove={removeLight(index)} />
        ))}
        <Inline>
          <Button disabled={disabled || full} onClick={() => onChange({ ...mat, lights: [...mat.lights, { ...NEW_LIGHT }] })}>
            Add a light
          </Button>
        </Inline>
        {full && <Note>{`A mat takes up to ${LIGHTS_MAX} lights.`}</Note>}
      </Stack>
    </FactGroup>
  );
}
