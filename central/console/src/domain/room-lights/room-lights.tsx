import { useState } from "react";

import {
  type ColourSensor, type ColourSensorView, type HeardEntityView, LIMITS, type RoomLight, type RoomLightsView,
  type RoomLightView, type Sensor, type SensorView,
} from "../../generated/contract";
import { EntityList } from "../../patterns/entity-list";
import { Note } from "../../patterns/fact-row";
import { OwnerLinks } from "../../patterns/link-to-owner";
import { type Outcome, RemoveDialog, SaveDialog } from "../../patterns/save-dialog";
import { TypedNumber } from "../../patterns/typed-number";
import { formatRoute } from "../../routes.js";
import { Button } from "../../ui/button";
import { Field, TextInput } from "../../ui/field";
import { SegmentedControl } from "../../ui/segmented-control";
import { Select } from "../../ui/select";
import { Inline, Stack } from "../../ui/stack";
import {
  brightnessWords, colourLineWords, colourReadingWords, colourSensorNames, entitiesFor, entityLabel, followedWords, heardWords,
  lightNames, NEW_KELVIN, newId, reportedThrough, reporterOf, reporterWords, sensorReadingWords, smartColourSensor,
  smartSensor, whereWords,
} from "./model";

export interface RoomLightsProps {
  view: RoomLightsView;
  /** The smart home's light, illuminance and colour-temperature entities as Central last heard them:
   * the pickers' choices. */
  heard: readonly HeardEntityView[];
  /** A Pi's name by its player id. */
  piName: (playerId: string) => string;
  /** Central's time in words. */
  since: (epochSeconds: number) => string;
  /** The last change's words, until the next. */
  note: string | null;
  onSaveLight: (id: string, light: RoomLight) => Outcome;
  onRemoveLight: (light: RoomLightView) => Outcome;
  onSaveSensor: (id: string, sensor: Sensor) => Outcome;
  onRemoveSensor: (sensor: SensorView) => Outcome;
  onSaveColourSensor: (id: string, sensor: ColourSensor) => Outcome;
  onRemoveColourSensor: (sensor: ColourSensorView) => Outcome;
}

const KELVIN = LIMITS.TypedColour.kelvin;
const SENSOR_PLACEMENT = "A Sensor reads every light that reaches it. Put it where the light it measures is the "
  + "strongest, and never facing the wall: the wall's own glow would read as room light.";
const NO_SMART_LIGHT = "No smart light has been heard yet: one that reports itself is offered here once it has.";
const NO_SMART_SENSOR = "No smart light sensor has been heard yet: one is offered here once it has.";
const NO_COLOUR_ENTITY = "No smart colour temperature sensor has been heard yet: one is offered here once it has.";
const NO_COLOUR_SENSOR = "Add a colour sensor first, then choose it here.";
const COLOUR_SENSOR_NOTE = "A colour sensor tells the colour of a light no bulb reports, as daylight through the "
  + "windows. A light a Sensor measures, or one always on, can take its colour from one.";

type Kind = RoomLight["kind"];
const KIND_LABELS: Record<Kind, string> = { reported: "Reports itself", measured: "Measured by a Sensor", fixed: "Always on" };
const KIND_HELP: Record<Kind, string> = {
  reported: "The light tells Photo Wall whether it is on and how bright.",
  measured: "A Sensor near it reads its light: for daylight, or a light with no switch Photo Wall hears.",
  fixed: "Always on, at full: a light nobody switches.",
};

type ColourSource = RoomLight["colour"]["kind"];
/** The colour sources each kind of light takes, its default first. */
const COLOUR_SOURCES: Record<Kind, readonly { value: ColourSource; label: string }[]> = {
  reported: [{ value: "its-bulb", label: "Its bulb's own" }, { value: "typed", label: "Typed" }],
  measured: [{ value: "typed", label: "Typed" }, { value: "colour-sensor", label: "A colour sensor" }],
  fixed: [{ value: "typed", label: "Typed" }, { value: "colour-sensor", label: "A colour sensor" }],
};
const UNTIL: Record<ColourSource, string> = {
  typed: "Colour temperature",
  "its-bulb": "Colour temperature until the bulb says",
  "colour-sensor": "Colour temperature until the colour sensor says",
};

interface LightDraft {
  name: string;
  kind: Kind;
  /** The smart light it reports itself through ("" before one is chosen). */
  entity: string;
  sensor: string;
  /** Where its colour comes from: typed, its bulb's own (a light that reports itself) or a colour
   * sensor (a light a Sensor measures, or one always on). */
  colour: ColourSource;
  /** The colour sensor it follows ("" before one is chosen). */
  colourSensor: string;
  kelvin: number;
}

/** `colour` if a light of `kind` takes it, else that kind's default source. */
const colourFor = (kind: Kind, colour: ColourSource): ColourSource =>
  (COLOUR_SOURCES[kind].some((each) => each.value === colour) ? colour : COLOUR_SOURCES[kind][0].value);

const lightDraft = (view: RoomLightView | null, house: RoomLightsView,
  lights: readonly HeardEntityView[]): LightDraft => {
  const light = view?.light ?? null;
  const colour = light?.colour ?? null;
  const kind = light?.kind ?? "fixed";
  return {
    name: light?.name ?? "",
    kind,
    entity: reporterOf(light) ?? lights[0]?.entity ?? "",
    sensor: light?.kind === "measured" ? light.sensor : house.sensors[0]?.id ?? "",
    colour: colourFor(kind, colour?.kind ?? "typed"),
    colourSensor: colour?.kind === "colour-sensor" ? colour.sensor : house.colour_sensors[0]?.id ?? "",
    kelvin: colour === null ? NEW_KELVIN : colour.kind === "typed" ? colour.kelvin : colour.typed_kelvin,
  };
};

/** The light a draft saves, or what is wrong with it. */
function lightOf(draft: LightDraft): { light: RoomLight } | { problem: string } {
  const name = draft.name.trim();
  if (name === "") return { problem: "Give the light a name, as Bookshelf spot." };
  const typed = { kind: "typed", kelvin: draft.kelvin } as const;
  if (draft.kind === "reported") {
    if (draft.entity === "") return { problem: "Choose the smart light that reports it." };
    const own = { kind: "its-bulb", typed_kelvin: draft.kelvin } as const;
    return { light: reportedThrough(name, draft.entity, draft.colour === "its-bulb" ? own : typed) };
  }
  if (draft.colour === "colour-sensor" && draft.colourSensor === "") return { problem: NO_COLOUR_SENSOR };
  const colour = draft.colour === "colour-sensor"
    ? { kind: "colour-sensor", sensor: draft.colourSensor, typed_kelvin: draft.kelvin } as const : typed;
  if (draft.kind === "measured") {
    if (draft.sensor === "") return { problem: "A Sensor measures this light: add one first, then choose it here." };
    return { light: { kind: "measured", name, sensor: draft.sensor, colour } };
  }
  return { light: { kind: "fixed", name, colour } };
}

function LightDialog({ view, house, heard, onClose, onSave }: {
  view: RoomLightView | null; house: RoomLightsView; heard: readonly HeardEntityView[]; onClose: () => void;
  onSave: RoomLightsProps["onSaveLight"];
}) {
  const smartLights = entitiesFor(heard, "light");
  const [first] = useState(() => lightDraft(view, house, smartLights));
  const [draft, setDraft] = useState(first);
  const before = view?.light ?? null;
  const kinds = (before?.kind === "reported" || smartLights.length > 0 ? ["reported", "measured", "fixed"] as const
    : ["measured", "fixed"] as const).map((kind) => ({ value: kind, label: KIND_LABELS[kind] }));
  const reported = draft.kind === "reported";
  // A light reporting through an entity not heard lately keeps it among the choices.
  const kept = reporterOf(before);
  const choices = [...smartLights.map((entity) => ({ value: entity.entity, label: entityLabel(entity) })),
    ...(kept !== null && !smartLights.some((entity) => entity.entity === kept) ? [{ value: kept, label: kept }] : [])];
  const save = async () => {
    const built = lightOf(draft);
    if ("problem" in built) return built.problem;
    const named = view === null ? newId(draft.name, house.lights.map((each) => each.id)) : { id: view.id };
    if ("problem" in named) return named.problem;
    return onSave(named.id, built.light);
  };
  return (
    <SaveDialog title={view === null ? "Add a room light" : `Change ${view.light.name}`}
      action={view === null ? "Add light" : "Save light"} onSave={save} onClose={onClose}>
      {({ busy, edited }) => {
        const change = (patch: Partial<LightDraft>) => { setDraft({ ...draft, ...patch }); edited(); };
        return (
          <>
            <Field label="Name">
              <TextInput value={draft.name} onChange={(name) => change({ name })} disabled={busy} />
            </Field>
            <Field label="How Photo Wall knows it is on" help={KIND_HELP[draft.kind]}>
              {/* Another kind takes its default colour source (its bulb's own for a light that reports
                  itself); back to the light's own kind, its own source. */}
              <SegmentedControl label="How Photo Wall knows it is on" value={draft.kind} options={kinds} disabled={busy}
                onChange={(kind) => change({ kind, colour: kind === before?.kind ? first.colour : COLOUR_SOURCES[kind][0].value })} />
            </Field>
            {smartLights.length === 0 && before?.kind !== "reported" && <Note>{NO_SMART_LIGHT}</Note>}
            {reported && (
              <Field label="Smart light" help="The light reports whether it is on and how bright through this">
                <Select value={draft.entity === "" ? null : draft.entity} disabled={busy} width="fit" options={choices}
                  onChange={(entity) => change({ entity })} />
              </Field>
            )}
            {draft.kind === "measured" && (house.sensors.length === 0 ? <Note>Add a Sensor first, then choose it here.</Note> : (
              <Field label="Sensor">
                <Select value={draft.sensor === "" ? null : draft.sensor} disabled={busy}
                  options={house.sensors.map((sensor) => ({ value: sensor.id, label: sensor.sensor.name }))}
                  onChange={(sensor) => change({ sensor })} />
              </Field>
            ))}
            <Field label="Colour">
              <SegmentedControl label="Colour" value={draft.colour} disabled={busy} options={COLOUR_SOURCES[draft.kind]}
                onChange={(colour) => change({ colour })} />
            </Field>
            {draft.colour === "colour-sensor" && (house.colour_sensors.length === 0 ? <Note>{NO_COLOUR_SENSOR}</Note> : (
              <Field label="Colour sensor">
                <Select value={draft.colourSensor === "" ? null : draft.colourSensor} disabled={busy}
                  options={house.colour_sensors.map((each) => ({ value: each.id, label: each.sensor.name }))}
                  onChange={(colourSensor) => change({ colourSensor })} />
              </Field>
            ))}
            <TypedNumber label={UNTIL[draft.colour]}
              help="2,700 K is a warm bulb, 6,500 K daylight" value={draft.kelvin}
              min={KELVIN.minimum} max={KELVIN.maximum} step={KELVIN.multipleOf} unit="K" disabled={busy}
              onChange={(kelvin) => change({ kelvin })} />
          </>
        );
      }}
    </SaveDialog>
  );
}

function SensorDialog({ view, onClose, onSave }: {
  view: SensorView; onClose: () => void; onSave: RoomLightsProps["onSaveSensor"];
}) {
  const [name, setName] = useState(view.sensor.name);
  const save = async () => {
    if (name.trim() === "") return "Give the Sensor a name, as Window Sensor.";
    return onSave(view.id, { ...view.sensor, name: name.trim() });
  };
  return (
    <SaveDialog title={`Change ${view.sensor.name}`} action="Save Sensor" onSave={save} onClose={onClose}>
      {({ busy, edited }) => (
        <Field label="Name">
          <TextInput value={name} onChange={(next) => { setName(next); edited(); }} disabled={busy} />
        </Field>
      )}
    </SaveDialog>
  );
}

function SmartSensorDialog({ house, heard, onClose, onSave }: {
  house: RoomLightsView; heard: readonly HeardEntityView[]; onClose: () => void; onSave: RoomLightsProps["onSaveSensor"];
}) {
  const meters = entitiesFor(heard, "illuminance");
  const [name, setName] = useState("");
  const [entity, setEntity] = useState(meters[0]?.entity ?? "");
  const save = async () => {
    if (name.trim() === "") return "Give the Sensor a name, as Window Sensor.";
    if (entity === "") return NO_SMART_SENSOR;
    const named = newId(name, house.sensors.map((each) => each.id));
    if ("problem" in named) return named.problem;
    return onSave(named.id, smartSensor(name.trim(), entity));
  };
  return (
    <SaveDialog title="Add a smart Sensor" action="Add Sensor" onSave={save} onClose={onClose}>
      {({ busy, edited }) => (
        <>
          <Field label="Name">
            <TextInput value={name} onChange={(next) => { setName(next); edited(); }} disabled={busy} />
          </Field>
          {meters.length === 0 ? <Note>{NO_SMART_SENSOR}</Note> : (
            <Field label="Smart light sensor" help="Its readings come from this from now on">
              <Select value={entity === "" ? null : entity} disabled={busy} width="fit"
                options={meters.map((each) => ({ value: each.entity, label: entityLabel(each) }))}
                onChange={(next) => { setEntity(next); edited(); }} />
            </Field>
          )}
        </>
      )}
    </SaveDialog>
  );
}

function ColourSensorDialog({ house, heard, onClose, onSave }: {
  house: RoomLightsView; heard: readonly HeardEntityView[]; onClose: () => void; onSave: RoomLightsProps["onSaveColourSensor"];
}) {
  const entities = entitiesFor(heard, "colour-temperature");
  const [name, setName] = useState("");
  const [entity, setEntity] = useState(entities[0]?.entity ?? "");
  const save = async () => {
    if (name.trim() === "") return "Give the colour sensor a name, as Sky colour.";
    if (entity === "") return NO_COLOUR_ENTITY;
    const named = newId(name, house.colour_sensors.map((each) => each.id));
    if ("problem" in named) return named.problem;
    return onSave(named.id, smartColourSensor(name.trim(), entity));
  };
  return (
    <SaveDialog title="Add a colour sensor" action="Add colour sensor" onSave={save} onClose={onClose}>
      {({ busy, edited }) => (
        <>
          <Field label="Name">
            <TextInput value={name} onChange={(next) => { setName(next); edited(); }} disabled={busy} />
          </Field>
          {entities.length === 0 ? <Note>{NO_COLOUR_ENTITY}</Note> : (
            <Field label="Smart colour temperature sensor" help="Its colour comes from this from now on">
              <Select value={entity === "" ? null : entity} disabled={busy} width="fit"
                options={entities.map((each) => ({ value: each.entity, label: entityLabel(each) }))}
                onChange={(next) => { setEntity(next); edited(); }} />
            </Field>
          )}
        </>
      )}
    </SaveDialog>
  );
}

type LightRow = RoomLightView & { key: string; severity: null };
type SensorRow = SensorView & { key: string; severity: null };
type ColourSensorRow = ColourSensorView & { key: string; severity: null };
type Editing = { kind: "light"; light: RoomLightView | null } | { kind: "sensor"; sensor: SensorView } | { kind: "smart-sensor" }
  | { kind: "colour-sensor" } | null;
type Removing = { kind: "light"; light: RoomLightView } | { kind: "sensor"; sensor: SensorView }
  | { kind: "colour-sensor"; sensor: ColourSensorView } | null;

/**
 * House › Lights: the house's real lights, each with how Photo Wall knows it is on, its colour line
 * (its colour and where it comes from), its last state and when it was last heard; the Sensors, each
 * with where it is, its last reading and the light it measures; the colour sensors, each with where
 * it is, its last colour and the lights that follow it. Lights are added, changed and removed here,
 * a smart light picked from the smart home's light entities; a smart Sensor is added here from its
 * illuminance entities, a wired one on its Pi's Hardware page; a colour sensor from its
 * colour-temperature entities. A fact its source can't tell is kept: "Can't tell since …". Central's
 * refusals in words. The page reads and writes; this is what a story draws.
 */
export function RoomLights({
  view, heard, piName, since, note, onSaveLight, onRemoveLight, onSaveSensor, onRemoveSensor, onSaveColourSensor,
  onRemoveColourSensor,
}: RoomLightsProps) {
  const [editing, setEditing] = useState<Editing>(null);
  const [removing, setRemoving] = useState<Removing>(null);
  const names = lightNames(view);
  const colourNames = colourSensorNames(view);
  const lights: LightRow[] = view.lights.map((light) => ({ ...light, key: light.id, severity: null }));
  const sensors: SensorRow[] = view.sensors.map((sensor) => ({ ...sensor, key: sensor.id, severity: null }));
  const colourSensors: ColourSensorRow[] = view.colour_sensors.map((sensor) => ({ ...sensor, key: sensor.id, severity: null }));
  return (
    <Stack>
      <Note>
        The real lights in the house. Photo Wall only watches them: each Frame lights its picture as these lights
        fall on a print in its place.
      </Note>
      <Stack>
        <EntityList<LightRow>
          label="Room lights"
          columns={[
            { id: "name", header: "Name", rowHeader: true, cell: (row) => row.light.name },
            { id: "reporter", header: "How it is known", cell: (row) => reporterWords(row.light, view.sensors) },
            { id: "colour", header: "Colour", cell: (row) => colourLineWords(row.colour, colourNames, since) },
            { id: "state", header: "Now", cell: (row) => brightnessWords(row, since) },
            { id: "heard", header: "Last heard", cell: (row) => heardWords(row, since) },
            {
              id: "actions", header: "Actions",
              cell: (row) => (
                <Inline>
                  <Button variant="ghost" aria-label={`Change ${row.light.name}`} onClick={() => setEditing({ kind: "light", light: row })}>Change</Button>
                  <Button variant="ghost" aria-label={`Remove ${row.light.name}`} onClick={() => setRemoving({ kind: "light", light: row })}>Remove</Button>
                </Inline>
              ),
            },
          ]}
          groups={[{ title: "In the house", rows: lights }]}
          empty="No room lights yet. Add the lights that fall on the wall: daylight, the overheads, a spotlight."
        />
        <Inline label="Room light actions">
          <Button onClick={() => setEditing({ kind: "light", light: null })}>Add a room light</Button>
        </Inline>
      </Stack>
      <Stack>
        <EntityList<SensorRow>
          label="Sensors"
          columns={[
            { id: "name", header: "Name", rowHeader: true, cell: (row) => row.sensor.name },
            { id: "at", header: "Where", cell: (row) => whereWords(row.sensor, piName) },
            { id: "reading", header: "Reading", cell: (row) => sensorReadingWords(row, since) },
            { id: "heard", header: "Last heard", cell: (row) => (row.heard_at === null ? "Never" : since(row.heard_at)) },
            { id: "reports", header: "Measures", cell: (row) => (row.reports === null ? "No light" : names[row.reports] ?? row.reports) },
            {
              id: "actions", header: "Actions",
              cell: (row) => (
                <Inline>
                  <Button variant="ghost" aria-label={`Change ${row.sensor.name}`} onClick={() => setEditing({ kind: "sensor", sensor: row })}>Change</Button>
                  <Button variant="ghost" aria-label={`Remove ${row.sensor.name}`} onClick={() => setRemoving({ kind: "sensor", sensor: row })}>Remove</Button>
                </Inline>
              ),
            },
          ]}
          groups={[{ title: "Sensors in the house", rows: sensors }]}
          empty="No Sensors. Wire one to a Pi on its Hardware page, or add a smart Sensor."
        />
        <Inline label="Sensor actions">
          <Button onClick={() => setEditing({ kind: "smart-sensor" })}>Add a smart Sensor</Button>
        </Inline>
        <Note>{SENSOR_PLACEMENT}</Note>
        <OwnerLinks links={[{ text: "Pis and their Sensors", href: formatRoute({ section: "hardware" }) }]} />
      </Stack>
      <Stack>
        <EntityList<ColourSensorRow>
          label="Colour sensors"
          columns={[
            { id: "name", header: "Name", rowHeader: true, cell: (row) => row.sensor.name },
            { id: "at", header: "Where", cell: (row) => whereWords(row.sensor, piName) },
            { id: "colour", header: "Colour", cell: (row) => colourReadingWords(row, since) },
            { id: "heard", header: "Last heard", cell: (row) => (row.heard_at === null ? "Never" : since(row.heard_at)) },
            { id: "followed", header: "Followed by", cell: (row) => followedWords(row, names) },
            {
              id: "actions", header: "Actions",
              cell: (row) => (
                <Button variant="ghost" aria-label={`Remove ${row.sensor.name}`}
                  onClick={() => setRemoving({ kind: "colour-sensor", sensor: row })}>Remove</Button>
              ),
            },
          ]}
          groups={[{ title: "Colour sensors in the house", rows: colourSensors }]}
          empty="No colour sensors. Add one to give daylight, or a light no bulb reports, its colour as it changes."
        />
        <Inline label="Colour sensor actions">
          <Button onClick={() => setEditing({ kind: "colour-sensor" })}>Add a colour sensor</Button>
        </Inline>
        <Note>{COLOUR_SENSOR_NOTE}</Note>
      </Stack>
      {note !== null && <Note live>{note}</Note>}
      {editing?.kind === "light" && (
        <LightDialog view={editing.light} house={view} heard={heard} onClose={() => setEditing(null)} onSave={onSaveLight} />
      )}
      {editing?.kind === "sensor" && (
        <SensorDialog view={editing.sensor} onClose={() => setEditing(null)} onSave={onSaveSensor} />
      )}
      {editing?.kind === "smart-sensor" && (
        <SmartSensorDialog house={view} heard={heard} onClose={() => setEditing(null)} onSave={onSaveSensor} />
      )}
      {editing?.kind === "colour-sensor" && (
        <ColourSensorDialog house={view} heard={heard} onClose={() => setEditing(null)} onSave={onSaveColourSensor} />
      )}
      {removing?.kind === "light" && (
        <RemoveDialog name={removing.light.light.name} what="Photo Wall forgets this light." onClose={() => setRemoving(null)}
          onRemove={() => onRemoveLight(removing.light)} />
      )}
      {removing?.kind === "sensor" && (
        <RemoveDialog name={removing.sensor.sensor.name} what="Photo Wall forgets this Sensor and its readings."
          onClose={() => setRemoving(null)} onRemove={() => onRemoveSensor(removing.sensor)} />
      )}
      {removing?.kind === "colour-sensor" && (
        <RemoveDialog name={removing.sensor.sensor.name} what="Photo Wall forgets this colour sensor and its colour."
          onClose={() => setRemoving(null)} onRemove={() => onRemoveColourSensor(removing.sensor)} />
      )}
    </Stack>
  );
}
