import React from "react";

import { explainPrecedence, nowShowing } from "./join.js";
import { FRAME_ID_PATTERN } from "./frameIds.js";
import { formatRoute, sceneCreationRoute } from "./routes.js";

/**
 * Now-showing facet (Bead 3, read-only).
 *
 * Two reads, both against the /runtime snapshot (`snapshot.runtime.current`):
 *
 *  1. The intended Scene — reuses the shared string-join primitive #4
 *     (`nowShowing`): the winning `visible` Intent whose target is the STRING
 *     `"frame:<id>"` (join.js). We surface only the `scene_id` (+ phase); there
 *     is no operator-facing scene "name", so we never invent one (design §5 J4).
 *
 *  2. The "why" — Central's plan for the frame (pass 2 slice 3 §10), read
 *     through `explainPrecedence` (join.js): who is on top, why each other
 *     layer is underneath (every line says "priority N"), and the limit line.
 *     The same explanation the Showrunner's Runs "Why" panel renders.
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function NowShowingFacet({ snapshot, frameId }) {
  const now = nowShowing(snapshot?.runtime, frameId);
  return (
    <div className="facet facet--nowshowing">
      <h3 className="facet__title">Now-showing</h3>
      {now === null ? (
        <p className="facet__empty">Nothing scheduled.</p>
      ) : (
        <p className="facet__scene">
          {`Intended scene: ${now.scene_id} (phase ${now.phase})`}
        </p>
      )}

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

/**
 * Central's plan for one frame, as `explainPrecedence` states it: the heading,
 * one row per layer (winner first), and the limit line, always shown.
 *
 * @param {{explanation: ReturnType<typeof explainPrecedence>, listLabel: string,
 *          listClass: string, emptyClass: string}} props
 */
export function PrecedenceExplanation({ explanation, listLabel, listClass, emptyClass }) {
  if (explanation === null) {
    return <p className={emptyClass}>No contributions target this frame.</p>;
  }
  return (
    <div className="precedence">
      <p className="precedence__heading">{explanation.heading}</p>
      <ol className={listClass} aria-label={listLabel}>
        {explanation.rows.map(({ intent, sentence }) => (
          <li key={`${intent.run_id}:${intent.target}`}>{sentence}</li>
        ))}
      </ol>
      <p className="precedence__limit">{explanation.limit}</p>
    </div>
  );
}
