import React, { useEffect, useRef } from "react";

import { BindingFacet } from "./BindingFacet.jsx";
import { Commissioning } from "./Commissioning.jsx";
import { frameHealth } from "./health.js";
import { NowShowingFacet } from "./NowShowingFacet.jsx";
import { ReadinessNotice } from "./ReadinessNotice.jsx";

/**
 * Frame Inspector shell (Bead 3, read-only) — shared primitive #6.
 *
 * A tabbed, read-only view of the selected Frame with three facets:
 * **Commissioning | Binding | Now-showing** (design §5 J3/J4). `facet` selects
 * the visible tab and defaults to "commissioning"; `onFacet(next)` is called
 * when the operator switches tabs (the open facet lives in the route,
 * `#/wall/frames/<id>/<facet>`, owned by WallPage.jsx). The facets are composed here as declarative JSX CHILDREN —
 * each is an ordinary component taking `({snapshot, frameId})` — rather than
 * registered through any imperative API.
 *
 * The Commissioning tab hosts the read-only Commissioning facet (Bead 4):
 * committed geometry + SDR gain, Frame facts, the Display as detected at the
 * last Player start, the bound Player/Output, and the capability-gated hardware
 * areas rendered "not yet available".
 *
 * Above the tabs, a heading names the frame and a health header states its
 * health from the one classifier (health.js) — the same label its plan tile
 * shows. When a visit from outside the plan (the attention strip, the Needs
 * attention page, the Equipment roster) issues a new `focusRequest`, the
 * heading takes focus, and the Inspector scrolls into view only if it is off
 * screen; plain selection passes no request and never moves focus. A request
 * issued before its frame is shown (the route changes a moment later) waits
 * for that frame's heading. A request is consumed once — `onFocusDone` clears it — so remounting the Inspector
 * (Wall → another section → Wall) never moves focus again.
 *
 * @typedef {"commissioning"|"binding"|"nowshowing"} Facet
 * With no frame selected (`frameId` null) it renders its empty state, "Select
 * a frame", so the Inspector column keeps its place in the layout.
 *
 * `bootFacts` (bootFacts.js, App-level) is passed through to the Binding facet.
 *
 * @param {{snapshot: object|null, bootFacts?: object|null, frameId: string|null, facet: Facet,
 *          onFacet: (facet: Facet) => void, focusRequest?: number|null,
 *          onFocusDone?: () => void}} props
 */
const FACETS = [
  { key: "commissioning", label: "Commissioning" },
  { key: "binding", label: "Binding" },
  { key: "nowshowing", label: "Now-showing" },
];

export function Inspector({
  snapshot,
  bootFacts = null,
  frameId,
  facet,
  onFacet,
  focusRequest = null,
  onFocusDone = () => {},
}) {
  const active = facet ?? "commissioning";
  const sectionRef = useRef(/** @type {HTMLElement|null} */ (null));
  const headingRef = useRef(/** @type {HTMLHeadingElement|null} */ (null));

  useEffect(() => {
    if (focusRequest === null || headingRef.current === null) {
      return;
    }
    headingRef.current.focus({ preventScroll: true });
    const box = sectionRef.current.getBoundingClientRect();
    const offScreen = box.top < 0 || box.top >= window.innerHeight || box.bottom <= 0;
    if (offScreen) {
      sectionRef.current.scrollIntoView({ block: "start" });
    }
    onFocusDone();
  }, [focusRequest, frameId]);
  const activeLabel = FACETS.find((entry) => entry.key === active)?.label ?? active;

  if (frameId === null) {
    return (
      <section className="inspector inspector--empty" role="region" aria-label="Inspector">
        <p className="inspector__empty">Select a frame</p>
      </section>
    );
  }
  const health = frameHealth(snapshot, frameId);

  return (
    <section
      ref={sectionRef}
      className="inspector"
      role="region"
      aria-label={`Frame ${frameId} inspector`}
    >
      <h2 ref={headingRef} className="inspector__title" tabIndex={-1}>
        {`Frame ${frameId}`}
      </h2>
      {health !== null && (
        <p className={`inspector__health health--${health.severity}`}>{health.label}</p>
      )}
      <ReadinessNotice snapshot={snapshot} frameId={frameId} />
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
          <Commissioning key={frameId} snapshot={snapshot} frameId={frameId} />
        )}
        {active === "binding" && (
          <BindingFacet
            key={frameId}
            snapshot={snapshot}
            bootFacts={bootFacts}
            frameId={frameId}
            onFacet={onFacet}
          />
        )}
        {active === "nowshowing" && (
          <NowShowingFacet snapshot={snapshot} frameId={frameId} />
        )}
      </div>
    </section>
  );
}
