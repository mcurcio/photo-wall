import React from "react";

/**
 * What a flow's section says when the route names an instance it cannot show as a
 * step (flow design §6; presentational), by `useFlowInstance`'s place:
 *
 *  - `blocked`: "Unsaved draft for X: Resume or Discard", then "Discarding it opens
 *    Y." with Resume and Discard (a typed or Back URL never replaces a dirty draft);
 *  - `missing`: "Y: This <noun> no longer exists.";
 *  - `unavailable`: "Y can't be edited here: <reason>".
 *
 * The last two link back to the section. Nothing renders for any other place.
 *
 * @param {{place: import("./instance.js").Place, draftName: string, targetName: string,
 *          noun: string, unavailableReason?: string, sectionHref: string,
 *          sectionLabel: string, onResume: () => void, onDiscard: () => void}} props
 */
export function InstanceNotice({
  place,
  draftName,
  targetName,
  noun,
  unavailableReason = "",
  sectionHref,
  sectionLabel,
  onResume,
  onDiscard,
}) {
  if (place === "blocked") {
    return (
      <div className="notice notice--warn">
        <p>{`Unsaved draft for ${draftName}: Resume or Discard`}</p>
        <p>{`Discarding it opens ${targetName}.`}</p>
        <div className="record__actions">
          <button type="button" className="button--primary" onClick={onResume}>
            Resume
          </button>
          <button type="button" onClick={onDiscard}>
            Discard
          </button>
        </div>
      </div>
    );
  }
  if (place !== "missing" && place !== "unavailable") {
    return null;
  }
  return (
    <div className="notice">
      <p>
        {place === "missing"
          ? `${targetName}: This ${noun} no longer exists.`
          : `${targetName} can't be edited here: ${unavailableReason}`}
      </p>
      <a href={sectionHref}>{`Back to ${sectionLabel}`}</a>
    </div>
  );
}

/**
 * The section's way into its flow (flow design §6; presentational): with a dirty
 * draft, "Unsaved draft for X." with "Resume draft (Draft)" and "Discard draft";
 * otherwise the section's New button (`newLabel`, `newRef`).
 *
 * @param {{dirty: boolean, draftName: string, newLabel: string,
 *          newRef?: React.Ref<HTMLButtonElement>, onNew: () => void,
 *          onResume: () => void,
 *          onDiscard: (event: React.MouseEvent<HTMLButtonElement>) => void}} props
 */
export function DraftBar({ dirty, draftName, newLabel, newRef, onNew, onResume, onDiscard }) {
  return (
    <div className="flow__toolbar">
      {dirty ? (
        <>
          <p className="flow__draft-note">{`Unsaved draft for ${draftName}.`}</p>
          <button type="button" className="button--primary" onClick={onResume}>
            Resume draft <span className="flow__draft-word">(Draft)</span>
          </button>
          <button type="button" onClick={onDiscard}>
            Discard draft
          </button>
        </>
      ) : (
        <button ref={newRef} type="button" className="button--primary" onClick={onNew}>
          {newLabel}
        </button>
      )}
    </div>
  );
}

/**
 * A flow running inline for another (flow/handOff.js; presentational): "This <noun> is
 * for <label>. Saving it takes you back there, with it chosen." and "Discard and return
 * to <label>" (`onDiscard`). Nothing renders without a pending hand-off. Not a live
 * region: it describes the page, it does not announce a change.
 *
 * @param {{handOff: {label: string}|null, noun: string,
 *          onDiscard: (event: React.MouseEvent<HTMLButtonElement>) => void}} props
 */
export function HandOffNotice({ handOff, noun, onDiscard }) {
  if (handOff === null) {
    return null;
  }
  return (
    <div className="notice">
      <p>{`This ${noun} is for ${handOff.label}. Saving it takes you back there, with it chosen.`}</p>
      <button type="button" onClick={onDiscard}>
        {`Discard and return to ${handOff.label}`}
      </button>
    </div>
  );
}
