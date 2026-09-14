import React, { useCallback } from "react";

import { apiWrite } from "./apiWrite.js";
import { SceneAuthoring } from "./SceneAuthoring.jsx";
import { useMutate } from "./useMutate.js";

/**
 * The Showrunner shell (Bead 12) — the "run the show" layer of the one console
 * (design §2, J4).
 *
 * It lays out the four show-programming REGIONS — Sources, Scenes, Programs,
 * Runs — as empty shells this bead; Beads 13-16 fill them. The only hardware
 * fact the show layer is allowed to see is per-Frame `calibration_valid`,
 * rendered here as a Frame-health BADGE (a STATUS, never a control): an invalid
 * Frame cannot present, so the showrunner needs the validity flag, but every
 * Display CONTROL stays behind the Wall-mode Commissioning facet (R4, J4).
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
  // A Source is a saved live QUERY named `name:rev` (design D-e) — NOT a
  // downloaded album, and never something a Player browses or links to. The
  // media plane carries the operator's saved queries; each renders as its
  // `name:rev` identity with a Refresh that re-runs the query server-side.
  const sources = snapshot?.media?.sources ?? [];
  const mutate = useMutate();

  // Refresh re-runs a saved query: POST …/sources/{ref}/refresh via the shared
  // apiWrite helper, wrapped in useMutate() (primitive #7) so Plane A — and
  // therefore this Sources list — refreshes exactly once after the write. The
  // ref is `name:rev` and may contain a colon, so it is path-encoded.
  const refreshSource = useCallback(
    (sourceRef) =>
      mutate(() =>
        apiWrite(
          `/v1/operator/sources/${encodeURIComponent(sourceRef)}/refresh`,
          { method: "POST" },
        ),
      ),
    [mutate],
  );

  return (
    <div className="showrunner" role="region" aria-label="Showrunner">
      {/* Frame-health badges: calibration_valid is STATUS, not a control (R4).
          One badge per Frame; an invalid Frame cannot present the show. */}
      <section
        className="showrunner__health"
        role="group"
        aria-label="Frame health"
      >
        {frames.map((frame) => {
          const valid = frame.calibration_valid === true;
          return (
            <span
              key={frame.id}
              className={`showrunner__badge showrunner__badge--${valid ? "valid" : "invalid"}`}
              aria-label={`Frame ${frame.id} calibration ${valid ? "valid" : "invalid"}`}
            >
              {`${frame.id}: ${valid ? "Calibration valid" : "Calibration invalid"}`}
            </span>
          );
        })}
      </section>

      <section className="showrunner__region" role="region" aria-label="Sources">
        <h2 className="showrunner__region-title">Sources</h2>
        {/* A Source is a saved live query (name:rev), re-run on Refresh — never
            a downloaded album, and the Player never sees it (design D-e). */}
        <p className="showrunner__region-note">
          Each Source is a saved live query. Refresh re-runs the query.
        </p>
        {sources.length === 0 ? (
          <p className="showrunner__empty">No Sources yet.</p>
        ) : (
          <ul className="showrunner__sources" role="list">
            {sources.map((source) => (
              <li key={source.source_ref} className="showrunner__source">
                <span className="showrunner__source-ref">
                  {source.source_ref}
                </span>
                <span className="showrunner__source-status">
                  {source.status}
                </span>
                <button
                  type="button"
                  className="showrunner__refresh"
                  aria-label={`Refresh ${source.source_ref}`}
                  onClick={() => refreshSource(source.source_ref)}
                >
                  Refresh
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="showrunner__region" role="region" aria-label="Scenes">
        <h2 className="showrunner__region-title">Scenes</h2>
        {/* Bead 14a: author + save a live-source Scene; the Scenes list appears
            here by scene_id. Bead 14b adds the authored per-frame mode. */}
        <SceneAuthoring snapshot={snapshot} />
      </section>
      <section className="showrunner__region" role="region" aria-label="Programs">
        <h2 className="showrunner__region-title">Programs</h2>
      </section>
      <section className="showrunner__region" role="region" aria-label="Runs">
        <h2 className="showrunner__region-title">Runs</h2>
      </section>
    </div>
  );
}
