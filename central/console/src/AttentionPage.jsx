import React from "react";

import { attentionView } from "./attention.js";
import { AttentionList } from "./AttentionList.jsx";
import { noneInDoubt, SourcesInDoubt, sourcesToShow } from "./domain/room-lights/sources-in-doubt.tsx";
import { LibraryHealthCard } from "./domain/library-health-card.tsx";
import { ReadinessNotice } from "./ReadinessNotice.jsx";
import { readinessRecoveryFrames } from "./readinessRecovery.js";
import { formatRoute, isPlainClick } from "./routes.js";
import { sinceTime } from "./timeWords.js";

/** Before the route's read of Central's errors answers: nothing said either way. */
const SOURCES_NOT_READ = Object.freeze({ view: null, failed: false });

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
 * Between the Frames and the readiness reports, the room lights Central is in doubt of (Central's
 * errors: one row per source reference, with what to do and the Frames that use it), from the
 * route's own read (`sources`, neutralRoutes.jsx). "Nothing needs attention" is said only once that
 * read answered with none: a failed or pending read is never "nothing".
 *
 * @param {{snapshot: object, central: {scheduler: string|null},
 *          wall: import("./wallState.js").WallMemory,
 *          hosts: import("./fleetHosts.js").FleetHosts|null, bootFacts: object|null,
 *          library?: {connections: Array<object>|null}|null,
 *          sources?: import("./domain/room-lights/sources-in-doubt.tsx").SourcesRead,
 *          lightNames?: Readonly<Record<string, string>>}} props
 */
export function AttentionPage({
  snapshot, central, wall, hosts, bootFacts, library = null, sources = SOURCES_NOT_READ, lightNames = {},
}) {
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
  const frameNames = new Map((snapshot?.inventory?.frames ?? []).map((frame) => [frame.id, frame.name ?? frame.id]));
  const frameLink = (frameId) => (
    <a
      className="attention-page__link"
      href={formatRoute(wall.frameRoute(frameId))}
      onClick={(event) => {
        if (isPlainClick(event)) wall.prepareVisit(frameId);
      }}
    >
      {frameNames.get(frameId) ?? frameId}
    </a>
  );
  const roomLights = sourcesToShow(sources) && (
    <section className="readiness-attention" aria-label="Room lights">
      <h2>Room lights</h2>
      <SourcesInDoubt read={sources} names={lightNames} since={(at) => sinceTime(at)} frameLink={frameLink}
        lightsHref={formatRoute({ section: "lights" })} />
    </section>
  );
  if (frameCount === 0) {
    return <>
      <p className="page__empty">No frames yet. Draw one on the Wall.</p>
      {roomLights}
      {photoLibrary}
    </>;
  }
  if (rows.length === 0 && readinessFrames.length === 0) {
    return <>
      {looks.length === 0 && noneInDoubt(sources) && <p className="page__empty">Nothing needs attention.</p>}
      {roomLights}
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
    {roomLights}
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
