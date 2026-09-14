import React from "react";

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
      </section>
      <section className="showrunner__region" role="region" aria-label="Scenes">
        <h2 className="showrunner__region-title">Scenes</h2>
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
