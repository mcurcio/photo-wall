import React from "react";

import { BindingFacet } from "./BindingFacet.jsx";
import { NowShowingFacet } from "./NowShowingFacet.jsx";

/**
 * Frame Inspector shell (Bead 3, read-only) — shared primitive #6.
 *
 * A tabbed, read-only view of the selected Frame with three facets:
 * **Commissioning | Binding | Now-showing** (design §5 J3/J4). `facet` selects
 * the visible tab and defaults to "commissioning"; `onFacet(next)` is called
 * when the operator switches tabs (the tab state itself lives in the caller's
 * Plane B, App.jsx). The facets are composed here as declarative JSX CHILDREN —
 * each is an ordinary component taking `({snapshot, frameId})` — rather than
 * registered through any imperative API.
 *
 * The Commissioning tab EXISTS now but its body is a placeholder: the real
 * Commissioning facet (hardware/geometry, capability-gated) lands in Bead 4.
 * This bead renders a stub there, never a hardware control.
 *
 * @typedef {"commissioning"|"binding"|"nowshowing"} Facet
 * @param {{snapshot: object|null, frameId: string, facet: Facet,
 *          onFacet: (facet: Facet) => void}} props
 */
const FACETS = [
  { key: "commissioning", label: "Commissioning" },
  { key: "binding", label: "Binding" },
  { key: "nowshowing", label: "Now-showing" },
];

export function Inspector({ snapshot, frameId, facet, onFacet }) {
  const active = facet ?? "commissioning";
  const activeLabel = FACETS.find((entry) => entry.key === active)?.label ?? active;

  return (
    <section
      className="inspector"
      role="region"
      aria-label={`Frame ${frameId} inspector`}
    >
      <div className="inspector__tabs" role="tablist" aria-label="Inspector facets">
        {FACETS.map(({ key, label }) => {
          const selected = key === active;
          return (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={selected}
              className={
                selected ? "inspector__tab inspector__tab--active" : "inspector__tab"
              }
              onClick={() => onFacet(key)}
            >
              {label}
            </button>
          );
        })}
      </div>

      <div
        className="inspector__body"
        role="tabpanel"
        aria-label={`${activeLabel} facet`}
      >
        {active === "commissioning" && (
          <p className="inspector__stub">Commissioning controls arrive in Bead 4.</p>
        )}
        {active === "binding" && (
          <BindingFacet snapshot={snapshot} frameId={frameId} />
        )}
        {active === "nowshowing" && (
          <NowShowingFacet snapshot={snapshot} frameId={frameId} />
        )}
      </div>
    </section>
  );
}
