import React from "react";

import { frameHealth } from "./health.js";
import { ProgramsRegion } from "./ProgramsRegion.jsx";
import { RunsRegion } from "./RunsRegion.jsx";
import { SceneAuthoring } from "./SceneAuthoring.jsx";
import { SourcesRegion } from "./SourcesRegion.jsx";

/**
 * The Showrunner — the "run the show" layer of the one console (design §2,
 * J4) — and its layout (pass 2 slice 3 §12). Two columns from 1024 px: **Now**
 * holds the Runs region with its Why panel; **Library** holds Scenes, Programs
 * and Sources. Narrower, one column with the Runs first. Each region lives in
 * its own module; this file only lays them out.
 *
 * The only hardware fact the show layer is allowed to see is each Frame's
 * health, from the one classifier (health.js) with the same label the wall
 * shows, rendered as a BADGE (a STATUS, never a control): a Frame that cannot
 * present matters to the showrunner, but every Display CONTROL stays behind
 * the Wall-mode Commissioning facet (R4, J4).
 *
 * R4 is enforced STRUCTURALLY by composition, not by a runtime `if (mode)`
 * guard: this component simply never imports or renders the Commissioning facet
 * (nor the Inspector that hosts it). There is therefore no code path — and no
 * DOM — by which Commissioning controls can appear in the show layer.
 *
 * @param {{snapshot: object|null}} props
 */
export function Showrunner({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  return (
    <div className="showrunner" role="region" aria-label="Showrunner">
      {/* Frame-health badges: STATUS, not a control (R4). One badge per Frame,
          labelled by the one classifier, exactly as the wall labels it. */}
      <section
        className="showrunner__health"
        role="group"
        aria-label="Frame health"
      >
        {frames.map((frame) => {
          const health = frameHealth(snapshot, frame.id);
          return (
            <span
              key={frame.id}
              className={`showrunner__badge health--${health.severity}`}
              aria-label={`Frame ${frame.id}: ${health.label}`}
            >
              {`${frame.id}: ${health.label}`}
            </span>
          );
        })}
      </section>

      <div className="showrunner__columns">
        <div className="showrunner__column showrunner__column--now">
          <RunsRegion snapshot={snapshot} />
        </div>
        <div className="showrunner__column showrunner__column--library">
          <section className="showrunner__region" role="region" aria-label="Scenes">
            <h2 className="showrunner__region-title">Scenes</h2>
            <SceneAuthoring snapshot={snapshot} />
          </section>
          <ProgramsRegion snapshot={snapshot} />
          <SourcesRegion snapshot={snapshot} />
        </div>
      </div>
    </div>
  );
}
