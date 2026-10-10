import React from "react";

import { formatRoute } from "./routes.js";
import { wallUnfinished } from "./unfinished.js";

// Each step's wording and the name of the mode or Frame page tab that fixes it (console DDD §62).
const STEPS = Object.freeze({
  place: { text: "not on the plan", link: "Edit layout" },
  bind: { text: "needs a Player", link: "Hardware" },
  calibrate: { text: "needs calibration", link: "Position" },
});

/**
 * The Wall's To finish list (console DDD §61, G1-G2): one row per unfinished step, in
 * Frame order, each a fact and ONE link to the mode or Frame page tab that fixes it. It hosts no
 * editor and no write, and renders nothing when the list is empty (no flag, no dismissal).
 *
 * @param {{snapshot: object|null}} props
 * @returns {JSX.Element|null}
 */
export function WallUnfinished({ snapshot }) {
  const items = wallUnfinished(snapshot);
  if (items.length === 0) {
    return null;
  }
  return (
    <section className="unfinished" aria-labelledby="wall-unfinished-title">
      <h2 id="wall-unfinished-title" className="unfinished__title">To finish</h2>
      <ul className="unfinished__list" aria-label="To finish">
        {items.map(({ frameId, step, route }) => (
          <li key={`${frameId}:${step}`} className="unfinished__item">
            {`${frameId} · ${STEPS[step].text} `}
            <a
              className="unfinished__link"
              href={formatRoute(route)}
              aria-label={`${STEPS[step].link}: ${frameId}`}
            >
              {STEPS[step].link}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
