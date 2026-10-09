import React from "react";

import { FactLine } from "./domain/fact-line.tsx";
import { FRAME_ID_PATTERN } from "./frameIds.js";
import { explainPrecedence, isBound, plannedFor } from "./join.js";
import { PrecedenceExplanation } from "./PrecedenceExplanation.jsx";
import { formatRoute, sceneCreationRoute } from "./routes.js";

/**
 * The Frame's Status facet (console DDD §34, §61; read-only, Wall-only).
 *
 * Two reads, both against the /runtime snapshot (`snapshot.runtime.current`):
 *
 *  1. The `planned` fact (join.js `plannedFor`, facts.js): which Run is on top on this
 *     Frame in Central's Runtime, who started it, and that media was not checked (or that
 *     an unbound Frame gets no layers). It is Central's projection, never what the Panel
 *     shows. Only the `scene_id` is named; there is no operator-facing Scene "name".
 *
 *  2. The "why" — Central's Runs on the frame (pass 2 slice 3 §10), read through
 *     `explainPrecedence` (join.js): who is on top, why each other layer is underneath
 *     (every line says "priority N"), and the limit line, rendered by the shared
 *     PrecedenceExplanation.jsx that the Show side's Runs "Why" panel renders too.
 *
 * `hostChip` is the Frame's host chip (domain/host-health-link.tsx, console DDD §61), rendered under the
 * title. The Wall's Inspector passes it in, so this facet imports no fleet host module and
 * its closure stays as R4 expects.
 *
 * @param {{snapshot: object|null, frameId: string, hostChip?: React.ReactNode}} props
 */
export function StatusFacet({ snapshot, frameId, hostChip = null }) {
  const frame = (snapshot?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const planned = plannedFor(snapshot?.runtime, frameId, isBound(frame));
  return (
    <div className="facet facet--status">
      <h3 className="facet__title">Status</h3>
      {hostChip}
      <div className="facet__planned">
        <FactLine fact={planned.fact} />
        {planned.phase === "outro" && <p className="facet__phase">Ending (outro)</p>}
      </div>

      <div className="facet__content-path">
        <h4 className="facet__subtitle">Put content on this Frame</h4>
        <p>
          Make a Scene; Frame {frameId} starts selected on its Frames step, and you can
          change the target Frames there. The Scene chooses the photos or videos; after
          saving it, choose Show now or Schedule it to put the Scene on screen.
        </p>
        <div className="record__actions">
          {FRAME_ID_PATTERN.test(frameId) ? (
            <a href={formatRoute(sceneCreationRoute(frameId))}>Make a Scene</a>
          ) : (
            <p role="status">
              This Frame id cannot be targeted by a Scene. Scene targets need an id of 96 characters or fewer without a colon.
            </p>
          )}
          <a href={formatRoute({ section: "scenes" })}>Browse Scenes</a>
        </div>
      </div>

      <h4 className="facet__subtitle">Why</h4>
      <PrecedenceExplanation
        explanation={explainPrecedence(snapshot?.runtime, frameId)}
        listLabel="Why"
        listClass="facet__why"
        emptyClass="facet__empty"
      />
    </div>
  );
}
