import React from "react";

import { nowShowing, rankedContributions } from "./join.js";

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
 *  2. The "why" — every `contribution` targeting this Frame, ranked by the total
 *     precedence order (priority, then root_order, then admission_order). The
 *     runtime keeps the MAX-precedence Intent as the visible winner
 *     (runtime.py:708), so this list is sorted DESCENDING: the winner (the
 *     intended Scene above) sits at the top. The filter reuses the SAME verified
 *     string join as primitive #4 — `intent.target === "frame:" + frameId` — not
 *     the object `{kind,id}` shape (which targets only the player protocol and
 *     would match nothing here). The order is total and deterministic.
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function NowShowingFacet({ snapshot, frameId }) {
  const now = nowShowing(snapshot?.runtime, frameId);

  // The "why" reuses the shared precedence read (primitive #4, join.js) so the
  // ranking rule lives in exactly one place — the same list the Showrunner Runs
  // "why" panel (Bead 16) renders. Sorted DESCENDING, so the visible winner tops.
  const why = rankedContributions(snapshot?.runtime, frameId);

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

      <h4 className="facet__subtitle">Why</h4>
      {why.length === 0 ? (
        <p className="facet__empty">No contributions target this frame.</p>
      ) : (
        <ol className="facet__why" aria-label="Why">
          {why.map((intent, index) => (
            <li key={`${intent.run_id}:${index}`}>
              {`${intent.scene_id} — priority ${intent.priority}, ` +
                `root order ${intent.root_order}, admission ${intent.admission_order}`}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
