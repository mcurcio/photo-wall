import React from "react";

import { AttentionList, attentionView } from "./AttentionList.jsx";
import { ReadinessNotice } from "./ReadinessNotice.jsx";
import { readinessRecoveryFrames } from "./readinessRecovery.js";
import { formatRoute, isPlainClick } from "./routes.js";

/**
 * The Needs attention page (#/attention): the attention strip's list at full width,
 * uncapped. Each frame's entry links to `#/wall/frames/<id>/<tab>`, the Frame page tab that
 * shows its cause (health.js `tabFor`); following it asks the Frame page to
 * take focus once, as the strip's own navigation does. A G1 list module (console DDD §49):
 * its closure reaches no page and no write (tests/test_console_routes_r4.py).
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          wall: import("./wallState.js").WallMemory,
 *          hosts: import("./fleetHosts.js").FleetHosts|null, bootFacts: object|null}} props
 */
export function AttentionPage({ snapshot, central, wall, hosts, bootFacts }) {
  const { frameCount, rows } = attentionView(snapshot, central, hosts, bootFacts);
  const readinessFrames = readinessRecoveryFrames(snapshot);
  if (frameCount === 0) {
    return <p className="page__empty">No frames yet. Draw one on the Wall.</p>;
  }
  if (rows.length === 0 && readinessFrames.length === 0) {
    return <p className="page__empty">Nothing needs attention.</p>;
  }
  return <>
    {rows.length > 0 && (
      <AttentionList
        className="attention-page__list"
        rows={rows}
        entry={(row) => (
          <a
            className="attention-page__link"
            href={formatRoute(wall.frameRoute(row.frameId))}
            onClick={(event) => {
              if (isPlainClick(event)) {
                wall.prepareVisit(row.frameId);
              }
            }}
          >
            {row.text}
          </a>
        )}
      />
    )}
    {readinessFrames.length > 0 && (
      <section className="readiness-attention" aria-label="Player readiness reports">
        <h2>Player readiness reports</h2>
        <ul>
          {readinessFrames.map(({ frameId }) => (
            <li key={frameId}>
              <a
                className="attention-page__link"
                href={formatRoute(wall.frameRoute(frameId))}
                onClick={(event) => {
                  if (isPlainClick(event)) wall.prepareVisit(frameId);
                }}
              >
                {`Frame ${frameId}`}
              </a>
              <ReadinessNotice snapshot={snapshot} frameId={frameId} />
            </li>
          ))}
        </ul>
      </section>
    )}
  </>;
}
