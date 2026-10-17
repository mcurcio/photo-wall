import type { HeardEntityView, HomeAssistantStatesView, RefusedReportView, RoomLightsView } from "../../generated/contract";
import { EntityList } from "../../patterns/entity-list";
import { FactRow, Note } from "../../patterns/fact-row";
import { OwnerLinks } from "../../patterns/link-to-owner";
import { formatRoute } from "../../routes.js";
import { Stack } from "../../ui/stack";
import { entitiesInOrder, entityName, heardWords, REFUSED_WORDS, reportsWords } from "./model";

export interface HomeAssistantProps {
  view: HomeAssistantStatesView;
  /** The house's room lights and Sensors, to say what each entity reports. */
  house: RoomLightsView;
  /** Central's time in words. */
  since: (epochSeconds: number) => string;
}

type EntityRow = HeardEntityView & { key: string; severity: null };
type RefusedRow = RefusedReportView & { key: string; severity: null };

/**
 * The Home Assistant settings page: when Central last heard Home Assistant's states; each light
 * and illuminance entity it published, by name with its entity id, with what it reports and when
 * it was last heard; and the newest states Central could not record, with why. Which entity
 * reports which room light or Sensor is picked on House › Lights. The page reads; this is what a
 * story draws.
 */
export function HomeAssistant({ view, house, since }: HomeAssistantProps) {
  const entities: EntityRow[] = entitiesInOrder(view).map((entity) => ({ ...entity, key: `${entity.kind}:${entity.entity}`, severity: null }));
  const refused: RefusedRow[] = view.refused.map((message, index) => ({ ...message, key: `${index}`, severity: null }));
  return (
    <Stack>
      <FactRow label="States last heard" tone="reported">{heardWords(view, since)}</FactRow>
      <Note>
        The Galleria integration for Home Assistant tells Photo Wall each light's and light sensor's state. Pick which
        one reports each room light or Sensor on House › Lights.
      </Note>
      <EntityList<EntityRow>
        label="Entities"
        columns={[
          { id: "name", header: "Entity", rowHeader: true, cell: (row) => entityName(row) },
          { id: "entity", header: "Entity id", cell: (row) => row.entity },
          { id: "reports", header: "Reports", cell: (row) => reportsWords(row, house) },
          { id: "heard", header: "Last heard", cell: (row) => (row.heard_at === null ? "Never" : since(row.heard_at)) },
        ]}
        groups={[{ title: "From Home Assistant", rows: entities }]}
        empty="No entities yet. Each appears here once Home Assistant reports it."
      />
      <OwnerLinks links={[{ text: "Room lights and Sensors", href: formatRoute({ section: "lights" }) }]} />
      <EntityList<RefusedRow>
        label="States not recorded"
        columns={[
          { id: "entity", header: "Entity id", rowHeader: true, cell: (row) => row.address },
          { id: "reason", header: "Why", cell: (row) => REFUSED_WORDS[row.reason] },
          { id: "at", header: "When", cell: (row) => since(row.at) },
        ]}
        groups={[{ title: "Newest first", rows: refused }]}
        empty="Every state was recorded."
      />
    </Stack>
  );
}
