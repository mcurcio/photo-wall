import { useState } from "react";

import { EntityList } from "../patterns/entity-list";
import { FactRow, Note } from "../patterns/fact-row";
import type {
  Colour, Reporter, RoomLightSave, RoomLightView, RoomLightsView, SensorPlace, SensorSave, SensorView,
} from "../roomLightsApi";
import {
  colourWords, ID_PATTERN, idFromName, placeWords, readingWords, reporterWords, stateWords,
} from "../roomLightWords.js";
import { sinceTime } from "../timeWords.js";
import { Button } from "../ui/button";
import { Dialog, DialogClose } from "../ui/dialog";
import { Field, NumberInput, TextInput } from "../ui/field";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
import { Select } from "../ui/select";
import { Inline, Stack } from "../ui/stack";

/** A Pi a Sensor may be wired to: its Registry id and its name ("Player …1a2b3c"). */
export interface PiChoice {
  id: string;
  name: string;
}

/** What a save or a remove came to: null when Central took it, else its words. */
export type Outcome = Promise<string | null>;

export interface RoomLightsProps {
  view: RoomLightsView;
  /** The Pis a Sensor may be wired to. */
  pis: readonly PiChoice[];
  /** The last change's words, until the next. */
  note: string | null;
  onSaveLight: (id: string, body: RoomLightSave) => Outcome;
  onRemoveLight: (light: RoomLightView) => Outcome;
  onSaveSensor: (id: string, body: SensorSave) => Outcome;
  onRemoveSensor: (sensor: SensorView) => Outcome;
}

// Kelvin as contracts/kelvin.py's table holds it (KELVIN_TABLE_MIN..MAX in KELVIN_TABLE_STEP).
const KELVIN = { min: 2000, max: 10000, step: 100 } as const;
const NEW_KELVIN = 2700;
const LINK_WORDS: Record<RoomLightsView["home_assistant"]["state"], string> = {
  "not-configured": "Not set up: Photo Wall has no Home Assistant broker address, so only Sensors on a Pi and "
    + "always-on lights report.",
  connected: "Connected",
  away: "Not connected",
};
const SENSOR_PLACEMENT = "A Sensor reads every light that reaches it. Put it where the light it reports is the "
  + "strongest, and never facing the wall: the wall's own glow would read as room light.";

type ReporterKind = Reporter["kind"];
const REPORTERS: readonly { value: ReporterKind; label: string }[] = [
  { value: "home-assistant-light", label: "Home Assistant" },
  { value: "sensor", label: "A Sensor" },
  { value: "fixed", label: "Always on" },
];
type PlaceKind = SensorPlace["kind"];
const PLACES: readonly { value: PlaceKind; label: string }[] = [
  { value: "pi", label: "On a Pi" },
  { value: "home-assistant", label: "Home Assistant" },
];

/** A room light's last-heard line: when Central recorded its state, "Always on" for a light nobody
 * reports, "Never" before its reporter's first word. */
export function heardWords(light: RoomLightView): string {
  if (light.heard_at !== null) return sinceTime(light.heard_at);
  return light.reporter.kind === "fixed" ? "Always on" : "Never";
}

/** The Home Assistant link in words. */
export function linkWords(link: RoomLightsView["home_assistant"]): string {
  const words = LINK_WORDS[link.state];
  return link.state === "away" && link.since !== null ? `${words} since ${sinceTime(link.since)}` : words;
}

interface LightDraft {
  name: string;
  reporter: ReporterKind;
  entity: string;
  sensor: string;
  colour: Colour["kind"];
  kelvin: string;
  shares: [string, string, string];
}

const lightDraft = (light: RoomLightView | null, sensors: readonly SensorView[]): LightDraft => ({
  name: light?.name ?? "",
  reporter: light?.reporter.kind ?? "home-assistant-light",
  entity: light?.reporter.kind === "home-assistant-light" ? light.reporter.entity : "",
  sensor: light?.reporter.kind === "sensor" ? light.reporter.sensor : sensors[0]?.id ?? "",
  colour: light?.colour.kind ?? "fixed",
  kelvin: String(light?.colour.kind === "fixed" ? light.colour.kelvin : NEW_KELVIN),
  shares: light?.colour.kind === "reported"
    ? light.colour.channel_shares.map((share) => String(Math.round(share * 100))) as [string, string, string]
    : ["34", "33", "33"],
});

/** The body a light draft saves, or what is wrong with it. */
function lightBody(draft: LightDraft): { body: RoomLightSave } | { problem: string } {
  const name = draft.name.trim();
  if (name === "") return { problem: "Give the light a name, as Bookshelf spot." };
  let reporter: Reporter;
  if (draft.reporter === "home-assistant-light") {
    if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test(draft.entity.trim())) {
      return { problem: "Type the light's Home Assistant entity, as light.bookshelf." };
    }
    reporter = { kind: "home-assistant-light", entity: draft.entity.trim() };
  } else if (draft.reporter === "sensor") {
    if (draft.sensor === "") return { problem: "Add a Sensor first, then choose it here." };
    reporter = { kind: "sensor", sensor: draft.sensor };
  } else {
    reporter = { kind: "fixed" };
  }
  let colour: Colour;
  if (draft.colour === "reported" && draft.reporter === "home-assistant-light") {
    const shares = draft.shares.map(Number);
    if (shares.some((share) => !Number.isFinite(share) || share < 0 || share > 100) || shares.every((share) => share === 0)) {
      return { problem: "Each colour share is a percent from 0 to 100, and they cannot all be 0." };
    }
    colour = { kind: "reported", channel_shares: shares.map((share) => share / 100) as [number, number, number] };
  } else {
    const kelvin = Number(draft.kelvin);
    if (!Number.isInteger(kelvin) || kelvin < KELVIN.min || kelvin > KELVIN.max || kelvin % KELVIN.step !== 0) {
      return { problem: `The colour is from ${KELVIN.min} K to ${KELVIN.max} K, in steps of ${KELVIN.step} K.` };
    }
    colour = { kind: "fixed", kelvin };
  }
  return { body: { name, reporter, colour } };
}

/** The id a new item gets, or what is wrong with its name. */
function newId(name: string, taken: readonly string[]): { id: string } | { problem: string } {
  const id = idFromName(name);
  if (!ID_PATTERN.test(id)) return { problem: "Give it a name with a letter or a digit." };
  if (taken.includes(id)) return { problem: `Another one already has the id ${id}: choose another name.` };
  return { id };
}

function LightDialog({ light, view, onClose, onSave }: {
  light: RoomLightView | null; view: RoomLightsView; onClose: () => void; onSave: RoomLightsProps["onSaveLight"];
}) {
  const [draft, setDraft] = useState(() => lightDraft(light, view.sensors));
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const change = (patch: Partial<LightDraft>) => { setDraft({ ...draft, ...patch }); setProblem(null); };
  const id = light?.id ?? idFromName(draft.name);
  const save = async () => {
    const built = lightBody(draft);
    const named = light === null ? newId(draft.name, view.lights.map((each) => each.id)) : { id: light.id };
    const wrong = "problem" in built ? built.problem : "problem" in named ? named.problem : null;
    if (wrong !== null || !("body" in built) || !("id" in named)) {
      setProblem(wrong);
      return;
    }
    setBusy(true);
    const refused = await onSave(named.id, built.body);
    setBusy(false);
    if (refused === null) onClose();
    else setProblem(refused);
  };
  const ha = draft.reporter === "home-assistant-light";
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }} busy={busy}
      title={light === null ? "Add a room light" : `Change ${light.name}`}
      actions={(
        <>
          <DialogClose>Cancel</DialogClose>
          <Button variant="primary" disabled={busy} onClick={() => void save()}>
            {light === null ? "Add light" : "Save light"}
          </Button>
        </>
      )}>
      <Stack>
        <Field label="Name" help={id === "" ? undefined : `Its id: ${id}`}>
          <TextInput value={draft.name} onChange={(name) => change({ name })} disabled={busy} />
        </Field>
        <Field label="Reported by" help="Who tells Photo Wall whether it is on: exactly one.">
          <SegmentedControl label="Reported by" value={draft.reporter} options={REPORTERS} disabled={busy}
            onChange={(reporter) => change({ reporter, colour: reporter === "home-assistant-light" ? draft.colour : "fixed" })} />
        </Field>
        {ha && (
          <Field label="Home Assistant entity" help="The light's entity id, as light.bookshelf">
            <TextInput value={draft.entity} onChange={(entity) => change({ entity })} disabled={busy} />
          </Field>
        )}
        {draft.reporter === "sensor" && (view.sensors.length === 0 ? <Note>Add a Sensor first, then choose it here.</Note> : (
          <Field label="Sensor">
            <Select value={draft.sensor === "" ? null : draft.sensor} disabled={busy}
              options={view.sensors.map((sensor) => ({ value: sensor.id, label: sensor.name }))}
              onChange={(sensor) => change({ sensor })} />
          </Field>
        ))}
        {draft.reporter === "fixed" && <Note>Always on, at full: daylight you never switch, a lamp left on.</Note>}
        {ha && (
          <Field label="Colour">
            <SegmentedControl label="Colour" value={draft.colour} disabled={busy}
              options={[{ value: "fixed", label: "White, in kelvin" }, { value: "reported", label: "Reported by the bulb" }]}
              onChange={(colour) => change({ colour })} />
          </Field>
        )}
        {draft.colour === "reported" && ha ? (
          <Inline label="Each screen colour's share of the bulb's white">
            {(["Red", "Green", "Blue"] as const).map((name, index) => (
              <Field key={name} label={`${name} share`} help="%">
                <NumberInput min={0} max={100} step={1} value={draft.shares[index]} disabled={busy}
                  onChange={(raw) => change({ shares: draft.shares.map((share, at) => (at === index ? raw : share)) as LightDraft["shares"] })} />
              </Field>
            ))}
          </Inline>
        ) : (
          <Field label="Colour temperature" help="2700 K is a warm bulb, 6500 K daylight">
            <NumberInput min={KELVIN.min} max={KELVIN.max} step={KELVIN.step} value={draft.kelvin} disabled={busy}
              onChange={(kelvin) => change({ kelvin })} />
          </Field>
        )}
        {problem !== null && <Note live>{problem}</Note>}
      </Stack>
    </Dialog>
  );
}

function SensorDialog({ sensor, view, pis, onClose, onSave }: {
  sensor: SensorView | null; view: RoomLightsView; pis: readonly PiChoice[]; onClose: () => void;
  onSave: RoomLightsProps["onSaveSensor"];
}) {
  const [name, setName] = useState(sensor?.name ?? "");
  const [place, setPlace] = useState<PlaceKind>(sensor?.at.kind ?? "pi");
  const [pi, setPi] = useState(sensor?.at.kind === "pi" ? sensor.at.player_id : pis[0]?.id ?? "");
  const [entity, setEntity] = useState(sensor?.at.kind === "home-assistant" ? sensor.at.entity : "");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const id = sensor?.id ?? idFromName(name);
  const save = async () => {
    const named = sensor === null ? newId(name, view.sensors.map((each) => each.id)) : { id: sensor.id };
    let wrong = name.trim() === "" ? "Give the Sensor a name, as Hall meter." : "problem" in named ? named.problem : null;
    if (wrong === null && place === "pi" && pi === "") wrong = "Choose the Pi the meter is wired to.";
    if (wrong === null && place === "home-assistant" && !/^[a-z0-9_]+\.[a-z0-9_]+$/.test(entity.trim())) {
      wrong = "Type the illuminance entity, as sensor.hall_illuminance.";
    }
    if (wrong !== null || !("id" in named)) {
      setProblem(wrong);
      return;
    }
    const at: SensorPlace = place === "pi" ? { kind: "pi", player_id: pi } : { kind: "home-assistant", entity: entity.trim() };
    setBusy(true);
    const refused = await onSave(named.id, { name: name.trim(), at });
    setBusy(false);
    if (refused === null) onClose();
    else setProblem(refused);
  };
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }} busy={busy}
      title={sensor === null ? "Add a Sensor" : `Change ${sensor.name}`}
      actions={(
        <>
          <DialogClose>Cancel</DialogClose>
          <Button variant="primary" disabled={busy} onClick={() => void save()}>
            {sensor === null ? "Add Sensor" : "Save Sensor"}
          </Button>
        </>
      )}>
      <Stack>
        <Field label="Name" help={id === "" ? undefined : `Its id: ${id}`}>
          <TextInput value={name} onChange={(next) => { setName(next); setProblem(null); }} disabled={busy} />
        </Field>
        <Field label="Where it is">
          <SegmentedControl label="Where it is" value={place} options={PLACES} disabled={busy}
            onChange={(next) => { setPlace(next); setProblem(null); }} />
        </Field>
        {place === "pi" ? (
          <Field label="Pi" help="The light meter wired to this Pi">
            <Select value={pi === "" ? null : pi} disabled={busy} options={pis.map((each) => ({ value: each.id, label: each.name }))}
              onChange={(next) => { setPi(next); setProblem(null); }} />
          </Field>
        ) : (
          <Field label="Home Assistant entity" help="An illuminance sensor's entity id, as sensor.hall_illuminance">
            <TextInput value={entity} onChange={(next) => { setEntity(next); setProblem(null); }} disabled={busy} />
          </Field>
        )}
        {problem !== null && <Note live>{problem}</Note>}
      </Stack>
    </Dialog>
  );
}

type Removing = { kind: "light"; light: RoomLightView } | { kind: "sensor"; sensor: SensorView };

function RemoveDialog({ removing, onClose, onRemove }: {
  removing: Removing; onClose: () => void; onRemove: () => Outcome;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const name = removing.kind === "light" ? removing.light.name : removing.sensor.name;
  const remove = async () => {
    setBusy(true);
    const refused = await onRemove();
    setBusy(false);
    if (refused === null) onClose();
    else setProblem(refused);
  };
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }} busy={busy} title={`Remove ${name}?`}
      actions={(
        <>
          <DialogClose>Cancel</DialogClose>
          <Button variant="danger" disabled={busy} onClick={() => void remove()}>Confirm remove</Button>
        </>
      )}>
      <Stack>
        <p>{removing.kind === "light" ? "Photo Wall forgets this light." : "Photo Wall forgets this Sensor and its readings."}</p>
        {problem !== null && <Note live>{problem}</Note>}
      </Stack>
    </Dialog>
  );
}

type LightRow = RoomLightView & { key: string; severity: null };
type SensorRow = SensorView & { key: string; severity: null };
type Editing = { kind: "light"; light: RoomLightView | null } | { kind: "sensor"; sensor: SensorView | null } | null;

/**
 * Settings › Lights (decision 0028): the house's room lights, each with its name, who reports it
 * (its Home Assistant light, a Sensor, or nobody: always on), its colour, its last state and when
 * it was last heard; the Sensors with where each is and its last reading; the Home Assistant link.
 * Add, change and remove each, Central's refusals in plain words. A room light's state line is its
 * state's, never worked out from when it was heard. The page reads and writes; this is what a
 * story draws.
 */
export function RoomLights({ view, pis, note, onSaveLight, onRemoveLight, onSaveSensor, onRemoveSensor }: RoomLightsProps) {
  const [editing, setEditing] = useState<Editing>(null);
  const [removing, setRemoving] = useState<Removing | null>(null);
  const sensorNames = Object.fromEntries(view.sensors.map((sensor) => [sensor.id, sensor.name]));
  const lightNames = Object.fromEntries(view.lights.map((light) => [light.id, light.name]));
  const piName = (id: string) => pis.find((pi) => pi.id === id)?.name ?? id;
  const lights: LightRow[] = view.lights.map((light) => ({ ...light, key: light.id, severity: null }));
  const sensors: SensorRow[] = view.sensors.map((sensor) => ({ ...sensor, key: sensor.id, severity: null }));
  return (
    <Stack>
      <Note>
        The real lights in the house. Photo Wall only watches them: each Frame lights its picture as these lights
        fall on a print in its place.
      </Note>
      <FactRow label="Home Assistant" tone="reported">{linkWords(view.home_assistant)}</FactRow>
      <Section title="Room lights">
        <EntityList<LightRow>
          label="Room lights"
          columns={[
            { id: "name", header: "Name", rowHeader: true, cell: (row) => row.name },
            { id: "reporter", header: "Reported by", cell: (row) => reporterWords(row.reporter, sensorNames) },
            { id: "colour", header: "Colour", cell: (row) => colourWords(row.colour) },
            { id: "state", header: "Now", cell: (row) => stateWords(row.state) },
            { id: "heard", header: "Last heard", cell: heardWords },
            {
              id: "actions", header: "Actions",
              cell: (row) => (
                <Inline>
                  <Button variant="ghost" aria-label={`Change ${row.name}`} onClick={() => setEditing({ kind: "light", light: row })}>Change</Button>
                  <Button variant="ghost" aria-label={`Remove ${row.name}`} onClick={() => setRemoving({ kind: "light", light: row })}>Remove</Button>
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
      </Section>
      <Section title="Sensors">
        <EntityList<SensorRow>
          label="Sensors"
          columns={[
            { id: "name", header: "Name", rowHeader: true, cell: (row) => row.name },
            { id: "at", header: "Where", cell: (row) => placeWords(row.at, piName) },
            { id: "reading", header: "Reading", cell: (row) => readingWords(row.reading) },
            { id: "heard", header: "Last heard", cell: (row) => (row.heard_at === null ? "Never" : sinceTime(row.heard_at)) },
            { id: "reports", header: "Reports", cell: (row) => (row.reports === null ? "No light" : lightNames[row.reports] ?? row.reports) },
            {
              id: "actions", header: "Actions",
              cell: (row) => (
                <Inline>
                  <Button variant="ghost" aria-label={`Change ${row.name}`} onClick={() => setEditing({ kind: "sensor", sensor: row })}>Change</Button>
                  <Button variant="ghost" aria-label={`Remove ${row.name}`} onClick={() => setRemoving({ kind: "sensor", sensor: row })}>Remove</Button>
                </Inline>
              ),
            },
          ]}
          groups={[{ title: "Light meters", rows: sensors }]}
          empty="No Sensors. A light meter on a Pi or in Home Assistant can report a light that has no switch, as daylight."
        />
        <Note>{SENSOR_PLACEMENT}</Note>
        <Inline label="Sensor actions">
          <Button onClick={() => setEditing({ kind: "sensor", sensor: null })}>Add a Sensor</Button>
        </Inline>
      </Section>
      {note !== null && <Note live>{note}</Note>}
      {editing?.kind === "light" && (
        <LightDialog light={editing.light} view={view} onClose={() => setEditing(null)} onSave={onSaveLight} />
      )}
      {editing?.kind === "sensor" && (
        <SensorDialog sensor={editing.sensor} view={view} pis={pis} onClose={() => setEditing(null)} onSave={onSaveSensor} />
      )}
      {removing !== null && (
        <RemoveDialog removing={removing} onClose={() => setRemoving(null)}
          onRemove={() => (removing.kind === "light" ? onRemoveLight(removing.light) : onRemoveSensor(removing.sensor))} />
      )}
    </Stack>
  );
}
