import React from "react";

import { attentionView } from "./attention.js";
import { AttentionList } from "./AttentionList.jsx";
import { LibraryHealthCard } from "./domain/library-health-card.tsx";
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
 * Below the Frames, one card per photo library connection that needs a look (roadmap 4, K1),
 * each with its one fix as a link to Settings › Photo library, where the fix is made. The
 * connections come from the route's own read (`library`, neutralRoutes.jsx), so this page reaches
 * no write.
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          wall: import("./wallState.js").WallMemory,
 *          hosts: import("./fleetHosts.js").FleetHosts|null, bootFacts: object|null,
 *          library?: {connections: Array<object>|null}|null}} props
 */
export function AttentionPage({ snapshot, central, wall, hosts, bootFacts, library = null }) {
  const { frameCount, rows } = attentionView(snapshot, central, hosts, bootFacts);
  const readinessFrames = readinessRecoveryFrames(snapshot);
  const looks = (library?.connections ?? []).filter((connection) => connection.health?.state === "needs_a_look");
  const photoLibrary = looks.length > 0 && (
    <section className="readiness-attention" aria-label="Photo library">
      <h2>Photo library</h2>
      {looks.map((connection) => (
        <LibraryHealthCard key={connection.connection_ref} connection={connection} />
      ))}
    </section>
  );
  if (frameCount === 0) {
    return <>
      <p className="page__empty">No frames yet. Draw one on the Wall.</p>
      {photoLibrary}
    </>;
  }
  if (rows.length === 0 && readinessFrames.length === 0) {
    return <>
      {looks.length === 0 && <p className="page__empty">Nothing needs attention.</p>}
      {photoLibrary}
    </>;
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
    {photoLibrary}
  </>;
}
