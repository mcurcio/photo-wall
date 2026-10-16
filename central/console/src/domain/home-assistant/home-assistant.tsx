import { useState } from "react";

import type { EntityView, HomeAssistantView, RefusedReportView, RoomLightsView } from "../../generated/contract";
import { EntityList } from "../../patterns/entity-list";
import { FactRow, Note } from "../../patterns/fact-row";
import { type Outcome, SaveDialog } from "../../patterns/save-dialog";
import { Button } from "../../ui/button";
import { Field, TextInput } from "../../ui/field";
import { SegmentedControl } from "../../ui/segmented-control";
import { Select } from "../../ui/select";
import { Stack } from "../../ui/stack";
import { entitiesInOrder, linkWords, REFUSED_WORDS, reportsWords } from "./model";

/** What an entity is linked to: one room light, an existing Sensor, or a new Sensor by name. */
export type Link = { kind: "light"; id: string } | { kind: "sensor"; id: string } | { kind: "new-sensor"; name: string };

export interface HomeAssistantProps {
  view: HomeAssistantView;
  /** The house's room lights and Sensors, to link an entity to. */
  house: RoomLightsView;
  /** Central's time in words. */
  since: (epochSeconds: number) => string;
  /** The last change's words, until the next. */
  note: string | null;
  /** Link `entity` to a room light or Sensor: null when Central took it, else its words. */
  onLink: (entity: string, link: Link) => Outcome;
}

type EntityRow = EntityView & { key: string; severity: null };
type RefusedRow = RefusedReportView & { key: string; severity: null };
const NEW_SENSOR = "new";

function LinkDialog({ entity, house, onClose, onLink }: {
  entity: EntityView; house: RoomLightsView; onClose: () => void; onLink: HomeAssistantProps["onLink"];
}) {
  const light = entity.kind === "light";
  const [target, setTarget] = useState(entity.reports ?? (light ? house.lights[0]?.id ?? ""
    : house.sensors[0]?.id ?? NEW_SENSOR));
  const [name, setName] = useState("");
  const save = async () => {
    let link: Link;
    if (light) {
      if (target === "") return "Add the room light on House › Lights first, then link it here.";
      link = { kind: "light", id: target };
    } else if (target === NEW_SENSOR) {
      if (name.trim() === "") return "Give the Sensor a name, as Window Sensor.";
      link = { kind: "new-sensor", name: name.trim() };
    } else {
      link = { kind: "sensor", id: target };
    }
    return onLink(entity.entity, link);
  };
  return (
    <SaveDialog title={`Link ${entity.entity}`} action="Link" onSave={save} onClose={onClose}>
      {({ busy, edited }) => (light ? (
        <Field label="Room light" help="This light reports itself through this entity from now on">
          <Select value={target === "" ? null : target} disabled={busy}
            options={house.lights.map((each) => ({ value: each.id, label: each.light.name }))}
            onChange={(next) => { setTarget(next); edited(); }} />
        </Field>
      ) : (
        <>
          <Field label="Sensor" help="This Sensor's readings come from this entity from now on">
            <SegmentedControl label="Sensor" value={target} disabled={busy}
              options={[...house.sensors.map((each) => ({ value: each.id, label: each.sensor.name })),
                { value: NEW_SENSOR, label: "A new Sensor" }]}
              onChange={(next) => { setTarget(next); edited(); }} />
          </Field>
          {target === NEW_SENSOR && (
            <Field label="Name">
              <TextInput value={name} onChange={(next) => { setName(next); edited(); }} disabled={busy} />
            </Field>
          )}
        </>
      ))}
    </SaveDialog>
  );
}

/**
 * The Home Assistant settings page: the link's health; each entity Home Assistant reported or a
 * room light or Sensor names, with what it reports and when it was last heard, linked to a room
 * light (a light entity) or a Sensor (an illuminance entity) here; and the newest messages the
 * link could not record, with why. The page reads and writes; this is what a story draws.
 */
export function HomeAssistant({ view, house, since, note, onLink }: HomeAssistantProps) {
  const [linking, setLinking] = useState<EntityView | null>(null);
  const entities: EntityRow[] = entitiesInOrder(view).map((entity) => ({ ...entity, key: `${entity.kind}:${entity.entity}`, severity: null }));
  const refused: RefusedRow[] = view.refused.map((message, index) => ({ ...message, key: `${index}`, severity: null }));
  return (
    <Stack>
      <FactRow label="Link" tone="reported">{linkWords(view.link, since)}</FactRow>
      <Note>
        Home Assistant tells Photo Wall when a light switches or a sensor reads, through the automation that publishes
        each entity. Link each entity to the room light or Sensor it reports.
      </Note>
      <EntityList<EntityRow>
        label="Entities"
        columns={[
          { id: "entity", header: "Entity", rowHeader: true, cell: (row) => row.entity },
          { id: "reports", header: "Reports", cell: (row) => reportsWords(row, house) },
          { id: "heard", header: "Last heard", cell: (row) => (row.heard_at === null ? "Never" : since(row.heard_at)) },
          {
            id: "actions", header: "Actions",
            cell: (row) => (
              <Button variant="ghost" aria-label={`Link ${row.entity}`} onClick={() => setLinking(row)}>Link</Button>
            ),
          },
        ]}
        groups={[{ title: "From Home Assistant", rows: entities }]}
        empty="No entities yet. Each appears here once Home Assistant reports it."
      />
      <EntityList<RefusedRow>
        label="Messages not recorded"
        columns={[
          { id: "topic", header: "Topic", rowHeader: true, cell: (row) => row.address },
          { id: "reason", header: "Why", cell: (row) => REFUSED_WORDS[row.reason] },
          { id: "at", header: "When", cell: (row) => since(row.at) },
        ]}
        groups={[{ title: "Newest first", rows: refused }]}
        empty="Every message was recorded."
      />
      {note !== null && <Note live>{note}</Note>}
      {linking !== null && (
        <LinkDialog entity={linking} house={house} onClose={() => setLinking(null)} onLink={onLink} />
      )}
    </Stack>
  );
}
