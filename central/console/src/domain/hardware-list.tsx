import type * as React from "react";

import type { Severity } from "../design/tokens";
import { type Fact, fact } from "../facts.js";
import { playersTable } from "../hostHealth.js";
import { type EntityColumn, type EntityGroup, EntityList, type EntityRowBase } from "../patterns/entity-list";
import { Note } from "../patterns/fact-row";
import { FocusFilter, FocusLink } from "../patterns/focus-filter";
import { HealthBadge } from "../patterns/health-badge";
import { OwnerLinks } from "../patterns/link-to-owner";
import { BOOT_FACTS_UNAVAILABLE, type PlayerRow, playersByDevice, RETIRED_NOT_READ } from "../players.js";
import { formatRoute, routeIdName } from "../routes.js";
import { hostsBusLink } from "./bus-link";
import { FactLine } from "./fact-line";
import { frameLinks } from "./frame-links";
import { HostFactsReceipt, HostItemLine, type HostLine, type HostsRead, hostVerdict } from "./host-health";

const NO_PIS = "No Pis yet. Power on one Pi on this network; it appears here.";

type Judged = ReturnType<typeof playersTable>[number];

/** One Pi's row: its box, its judged host values (null when not read) and its link record. */
interface HardwareRow extends EntityRowBase {
  pi: PlayerRow;
  judged: Judged;
  busLink: Fact;
}

const items = (list: readonly HostLine[] | undefined, names: readonly string[]) =>
  names.flatMap((name) => (list ?? []).filter((item) => (item as { name?: string }).name === name));

function Lines({ lines, receipt = true }: { lines: readonly HostLine[]; receipt?: boolean }) {
  return <>{lines.map((entry) => <HostItemLine key={entry.label + factKey(entry)} entry={entry} receipt={receipt} />)}</>;
}
const factKey = (entry: HostLine) => (entry as { name?: string }).name ?? "";

/** The host columns (console by domain § Fleet, Hardware): each names the items it shows. */
function hostColumns(): EntityColumn<HardwareRow>[] {
  const health = (row: HardwareRow) => row.judged.health?.items as HostLine[] | undefined;
  const facts = (row: HardwareRow) => row.judged.facts?.items as HostLine[] | undefined;
  return [
    { id: "link", header: "Link", cell: (row) => (
      <>
        <FactLine label="Node API link" fact={row.busLink} />
        <Lines lines={items(health(row), ["host"])} />
      </>
    ) },
    { id: "thermal", header: "Temperature, throttling", cell: (row) => (
      <Lines lines={items(health(row), ["soc_temperature", "throttling"])} />
    ) },
    { id: "compute", header: "CPU, /run storage", cell: (row) => (
      <Lines lines={items(health(row), ["cpu_busy", "runtime_available", "preparation", "out_of_memory",
        "memory_limits"])} />
    ) },
    { id: "network", header: "Network", cell: (row) => (
      <>
        <Lines lines={items(facts(row), ["link", "link_state"])} receipt={false} />
        <Lines lines={items(health(row), ["link_speed"])} />
        <Lines lines={items(facts(row), ["address"])} receipt={false} />
        {row.judged.facts && <HostFactsReceipt receipt={row.judged.facts.receipt} />}
      </>
    ) },
  ];
}

function baseColumns(): EntityColumn<HardwareRow>[] {
  return [
    { id: "player", header: "Player", rowHeader: true, cell: (row) => (
      <>
        <a href={formatRoute({ section: "hardware", id: row.pi.deviceId })}>{row.pi.name}</a>
        <FocusLink href={formatRoute({ section: "hardware", pi: row.pi.deviceId })} name={row.pi.name} />
      </>
    ) },
    { id: "standing", header: "Standing", cell: (row) => (
      <>
        <FactLine label="Standing" fact={fact({ kind: "set", value: row.pi.standingLabel })} />
        {row.judged.group === "bound" && row.judged.health !== null && (
          <HealthBadge verdict={hostVerdict(row.judged.health)} />
        )}
        <OwnerLinks links={frameLinks(row.pi.frames)} />
      </>
    ) },
  ];
}

const RETIRED_COLUMN: EntityColumn<HardwareRow> = {
  id: "values", header: "Host values", cell: () => <Note>{RETIRED_NOT_READ}</Note>,
};

export interface HardwareListProps {
  snapshot: object | null;
  bootFacts: { unavailable?: boolean; devices?: Map<string, object> } | null;
  /** The shell's fleet host read; null while it is skipped (node control not on). */
  hosts: HostsRead | null;
  /** The Pi the list is focused on (`?pi=`), or null. */
  focus: string | null;
}

/**
 * The Hardware list (console by domain § Fleet): one row per Pi, keyed by its device identity
 * (players.js `playersByDevice`) and judged by hostHealth.js `playersTable`, in three groups:
 * Driving a Frame (Bound Players, worst first by their host tier), Not driving a Frame (boxes
 * seen at boot, then Unbound Players newest first; never judged, G2) and Retired (not read). It
 * sends no node read of its own: the host columns come from the shell's one fleet host read and
 * are not shown while that read is skipped. A focus (`?pi=`) narrows every group to that Pi.
 */
export function HardwareList({ snapshot, bootFacts, hosts, focus }: HardwareListProps) {
  const pis = playersByDevice(snapshot, bootFacts as never);
  const judged = new Map(playersTable(pis, hosts).map((entry) => [entry.row.deviceId, entry]));
  const toRow = (pi: PlayerRow): HardwareRow => {
    const entry = judged.get(pi.deviceId) as Judged;
    return {
      key: pi.deviceId, pi, judged: entry,
      severity: (entry.group === "bound" ? entry.tier : null) as Severity | null,
      busLink: hostsBusLink(hosts, pi.deviceId),
    };
  };
  const shown = focus === null ? pis : pis.filter((pi) => pi.deviceId === focus);
  const of = (standing: string) => shown.filter((pi) => pi.standing === standing);
  const columns = [...baseColumns(), ...(hosts === null ? [] : hostColumns())];
  const groups: EntityGroup<HardwareRow>[] = [
    { title: "Driving a Frame", rows: of("bound").map(toRow), worstFirst: true },
    { title: "Not driving a Frame", rows: [...of("not-enrolled"), ...of("unbound").reverse()].map(toRow) },
    { title: "Retired", rows: of("retired").map(toRow),
      columns: hosts === null ? baseColumns() : [...baseColumns(), RETIRED_COLUMN] },
  ];
  const focused = focus === null ? null
    : pis.find((pi) => pi.deviceId === focus)?.name ?? routeIdName("Pi", focus);
  const notes: React.ReactNode = (
    <>
      <FocusFilter focused={focused} noun="Pi" clearHref={formatRoute({ section: "hardware" })} />
      {bootFacts?.unavailable && (
        <Note>{`${BOOT_FACTS_UNAVAILABLE}; serials and boxes seen only at boot may be out of date.`}</Note>
      )}
      {hosts?.failed && <Note>{`Host health: last read failed: ${hosts.error?.code ?? "unanswered"}`}</Note>}
    </>
  );
  return (
    <EntityList label="Pis" columns={columns} groups={groups} notes={notes}
      empty={focus === null ? NO_PIS : `${focused} is not known to Central.`} />
  );
}
