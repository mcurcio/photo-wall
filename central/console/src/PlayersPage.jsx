import React from "react";

import { FactLine } from "./FactLine.jsx";
import { fact } from "./facts.js";
import { BOOT_FACTS_UNAVAILABLE } from "./health.js";
import { playersByDevice } from "./players.js";
import { formatRoute, isPlainClick } from "./routes.js";
import { V1FleetPolicy } from "./V1Offers.jsx";

const NO_PLAYERS = "No Players yet. Power on one Pi on this network; it appears here.";

/**
 * The Players list (`#/players`; console DDD §9): one row per box, keyed by its device
 * identity (players.js `playersByDevice`), each linking to its Player page, with its
 * standing and the Frames its Outputs are bound to. A box seen only at boot reads "Not
 * enrolled". The list does NO node read: node state lives on each Player page.
 *
 * Above it, the V1 fleet policy (V1Offers.jsx `V1FleetPolicy`, labelled "V1 boot offers");
 * each Player's own V1 records are on its Player page.
 *
 * @param {{snapshot: object, bootFacts: object|null,
 *          wall: import("./wallState.js").WallMemory}} props
 */
export function PlayersPage({ snapshot, bootFacts, wall }) {
  const rows = playersByDevice(snapshot, bootFacts);
  return (
    <>
      <V1FleetPolicy snapshot={snapshot} />
      <section className="roster" role="region" aria-label="Players list">
        <h2 className="roster__title">Players list</h2>
        {bootFacts?.unavailable && (
          <p className="roster__note">{`${BOOT_FACTS_UNAVAILABLE}; serials and boxes seen only at boot may be out of date.`}</p>
        )}
        {rows.length === 0 ? (
          <p className="roster__empty">{NO_PLAYERS}</p>
        ) : (
          <ul className="roster__cards players__list" aria-label="Players">
            {rows.map((row) => (
              <li key={row.deviceId} className="roster__card">
                <a
                  className="roster__player"
                  href={formatRoute({ section: "players", id: row.deviceId })}
                >
                  {row.name}
                </a>
                <FactLine label="Standing" fact={fact({ kind: "set", value: row.standingLabel })} />
                {row.frames.length > 0 && (
                  <p className="roster__standing">
                    {"Bound to "}
                    {row.frames.map((entry, index) => (
                      <React.Fragment key={entry.frameId}>
                        {index > 0 && ", "}
                        <a
                          href={formatRoute(wall.frameRoute(entry.frameId))}
                          onClick={(event) => {
                            if (isPlainClick(event)) wall.prepareVisit(entry.frameId);
                          }}
                        >
                          {`Frame ${entry.frameId}`}
                        </a>
                      </React.Fragment>
                    ))}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
