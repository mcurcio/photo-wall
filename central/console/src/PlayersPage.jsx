import React from "react";

import { FactLine } from "./FactLine.jsx";
import { fact } from "./facts.js";
import { BOOT_FACTS_UNAVAILABLE } from "./health.js";
import { HostFactsReceipt, HostItem } from "./HostHealthSection.jsx";
import { judgeHost, playersTable } from "./hostHealth.js";
import { playersByDevice, RETIRED_NOT_READ } from "./players.js";
import { formatRoute, isPlainClick } from "./routes.js";

const NO_PLAYERS = "No Players yet. Power on one Pi on this network; it appears here.";

// The host columns (§61), each naming the classifyHost items (`metrics`) and host facts
// items (`facts`) it shows, in order. Network ends with the facts record's one receipt line.
const HOST_COLUMNS = Object.freeze([
  { title: "Host Management", metrics: ["host"], facts: [] },
  { title: "Temperature", metrics: ["soc_temperature"], facts: [] },
  { title: "Throttling", metrics: ["throttling"], facts: [] },
  { title: "CPU", metrics: ["cpu_busy"], facts: [] },
  { title: "Storage", metrics: ["runtime_available", "preparation", "out_of_memory", "memory_limits"],
    facts: [] },
  { title: "Network", metrics: ["link_speed"], facts: ["link", "link_state", "address"], receipt: true },
  { title: "Software", metrics: ["boot_preparation", "base_units"],
    facts: ["kernel", "base_reported", "base", "base_mismatch"] },
].map(Object.freeze));

/** One host column's cell for a box: its items, as the one classifier words them. */
function HostCell({ column, health, facts }) {
  const metrics = column.metrics.flatMap((name) => health.items.filter((item) => item.name === name));
  const named = column.facts.flatMap((name) => facts.items.filter((item) => item.name === name));
  return (
    <td className="players__cell">
      {named.filter((item) => item.name !== "address").map((item) => (
        <HostItem key={item.name} entry={item} receipt={false} />
      ))}
      {metrics.map((item) => <HostItem key={item.name} entry={item} />)}
      {named.filter((item) => item.name === "address").map((item) => (
        <HostItem key={item.name} entry={item} receipt={false} />
      ))}
      {column.receipt && <HostFactsReceipt receipt={facts.receipt} />}
    </td>
  );
}

/** A box's Frames as chips, each a link to the Frame's home on the Wall. */
function FrameChips({ frames, wall }) {
  if (frames.length === 0) return null;
  return (
    <span className="players__frames">
      {frames.map((entry) => (
        <a
          key={entry.frameId}
          className="players__chip"
          href={formatRoute(wall.frameRoute(entry.frameId))}
          onClick={(event) => {
            if (isPlainClick(event)) wall.prepareVisit(entry.frameId);
          }}
        >
          {`Frame ${entry.frameId}`}
        </a>
      ))}
    </span>
  );
}

const playerLink = (row) => (
  <a className="roster__player" href={formatRoute({ section: "players", id: row.deviceId })}>{row.name}</a>
);

/**
 * Not driving a Frame (§54, §61): Unbound Players and boxes seen at boot but never enrolled,
 * filtered from the rows `playersByDevice` produced, each with its hint from the fleet host
 * read. Listed, never counted, and never a gate. Newest first: boot facts serve no first-boot
 * time, so boxes seen at boot come first (in `playersByDevice`'s order), then Unbound Players
 * newest registration first (`playersByDevice` lists them oldest first).
 */
function NotDrivingAFrame({ rows, hosts }) {
  const spares = [
    ...rows.filter((row) => row.standing === "not-enrolled"),
    ...rows.filter((row) => row.standing === "unbound").reverse(),
  ];
  if (spares.length === 0) return null;
  return (
    <section className="players__spares" role="region" aria-label="Not driving a Frame">
      <h3 className="roster__group-title">Not driving a Frame</h3>
      <ul className="roster__cards" aria-label="Players not driving a Frame">
        {spares.map((row) => {
          const hint = hosts === null ? undefined
            : judgeHost(hosts, row.deviceId).health.items.find((item) => item.name === "host");
          return (
            <li key={row.deviceId} className="roster__card">
              {playerLink(row)}
              <FactLine label="Standing" fact={fact({ kind: "set", value: row.standingLabel })} />
              {hint !== undefined && <FactLine label={hint.label} fact={hint.fact} suffix={hint.suffix} />}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * The Players list (`#/players`; console DDD §52, §61): ONE table, one row per box, keyed by
 * its device identity (players.js `playersByDevice`), ordered and judged by hostHealth.js
 * `playersTable`: Bound Players worst first by their `classifyHost` tier, then spares unbanded
 * (G2: never alarmed), then retired boxes, each stating RETIRED_NOT_READ. It scrolls sideways at phone width; there is no second
 * rendering and no counts line (the strip counts incidents). The list sends no node read of
 * its own: the host columns come from the shell's one fleet host read (`hosts`,
 * fleetHosts.js) and are not shown while that read is skipped (node control not on).
 * Below the table, the boxes not driving a Frame.
 *
 * @param {{snapshot: object, bootFacts: object|null,
 *          wall: import("./wallState.js").WallMemory,
 *          hosts?: import("./fleetHosts.js").FleetHosts|null}} props
 */
export function PlayersPage({ snapshot, bootFacts, wall, hosts = null }) {
  const rows = playersByDevice(snapshot, bootFacts);
  const judged = playersTable(rows, hosts);
  return (
    <>
      <section className="roster" role="region" aria-label="Players list">
        <h2 className="roster__title">Players list</h2>
        {bootFacts?.unavailable && (
          <p className="roster__note">{`${BOOT_FACTS_UNAVAILABLE}; serials and boxes seen only at boot may be out of date.`}</p>
        )}
        {hosts?.failed && (
          <p className="roster__note">{`Host health: last read failed: ${hosts.error?.code ?? "unanswered"}`}</p>
        )}
        {rows.length === 0 ? (
          <p className="roster__empty">{NO_PLAYERS}</p>
        ) : (
          <div className="players__scroll" role="region" aria-label="Players table" tabIndex={0}>
            <table className="players__table" aria-label="Players">
              <thead>
                <tr>
                  <th scope="col">Player</th>
                  <th scope="col">Standing and Frames</th>
                  {hosts !== null && HOST_COLUMNS.map((column) => <th key={column.title} scope="col">{column.title}</th>)}
                </tr>
              </thead>
              <tbody>
                {judged.map(({ row, group, tier, health, facts }) => (
                  <tr key={row.deviceId} className={`players__row players__row--${tier ?? "none"}`}>
                    <th scope="row">{playerLink(row)}</th>
                    <td className="players__cell">
                      <FactLine label="Standing" fact={fact({ kind: "set", value: row.standingLabel })} />
                      <FrameChips frames={row.frames} wall={wall} />
                    </td>
                    {hosts !== null && (group === "retired" ? (
                      <td className="players__cell" colSpan={HOST_COLUMNS.length}>
                        <p className="player__read-time">{RETIRED_NOT_READ}</p>
                      </td>
                    ) : HOST_COLUMNS.map((column) => (
                      <HostCell key={column.title} column={column} health={health} facts={facts} />
                    )))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <NotDrivingAFrame rows={rows} hosts={hosts} />
    </>
  );
}
