import React from "react";

/**
 * Binding facet (Bead 3, read-only).
 *
 * Shows the Player/Output currently serving the selected Frame, read straight
 * from the Frame's FrameInventory row (`player_id`/`output_id`). A Frame with no
 * binding (either id null) is reported "unbound" — the console never invents a
 * Player or Output that the inventory does not carry (design §5 J3, R1: every
 * fact is read through the Binding to the Frame).
 *
 * @param {{snapshot: object|null, frameId: string}} props
 */
export function BindingFacet({ snapshot, frameId }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const frame = frames.find((candidate) => candidate.id === frameId);
  const bound = frame != null && frame.player_id != null && frame.output_id != null;

  return (
    <div className="facet facet--binding">
      <h3 className="facet__title">Binding</h3>
      {bound ? (
        <dl className="facet__fields">
          <div className="facet__field">
            <dt>Player</dt>
            <dd>{frame.player_id}</dd>
          </div>
          <div className="facet__field">
            <dt>Output</dt>
            <dd>{frame.output_id}</dd>
          </div>
        </dl>
      ) : (
        <p className="facet__empty">Unbound</p>
      )}
    </div>
  );
}
