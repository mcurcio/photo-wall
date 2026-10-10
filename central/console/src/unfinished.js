import { isBound } from "./join.js";
import { isUnplaced } from "./projection.js";

/**
 * The Wall's To finish list (console DDD §54, G2): the one pure model of the Wall's
 * unfinished bring-up steps, read from `set` records only.
 *
 *  - "place": the Frame is not on the plan (the Unplaced tray's rule, projection.js
 *    `isUnplaced`); fixed in Edit layout.
 *  - "bind": no Binding exists; fixed on the Frame page's Hardware tab.
 *  - "calibrate": bound, and `calibration_valid` is not true; fixed on the Frame page's
 *    Position tab. An unbound Frame has no position to save, so it is never asked for one.
 *
 * No `reported` or `derived` fact (liveness, readiness, host health) adds or removes an
 * item: those are Needs attention's incidents (health.js `wallAttention`). Nothing counts
 * this list and no route depends on it; it empties itself as the records arrive.
 *
 * @typedef {"place"|"bind"|"calibrate"} Step
 * @typedef {{frameId: string, step: Step, route: import("./routes.js").Route}} Unfinished
 *
 * @param {object|null} snapshot
 * @returns {Unfinished[]} in Frame-id order, each Frame's steps in bring-up order
 */
export function wallUnfinished(snapshot) {
  const frames = [...(snapshot?.inventory?.frames ?? [])].sort((a, b) =>
    a.id < b.id ? -1 : a.id > b.id ? 1 : 0,
  );
  const items = [];
  for (const frame of frames) {
    if (isUnplaced(frame)) {
      items.push({ frameId: frame.id, step: "place", route: { section: "wall", mode: "layout" } });
    }
    if (!isBound(frame)) {
      items.push({ frameId: frame.id, step: "bind",
        route: { section: "wall", id: frame.id, tab: "hardware" } });
    } else if (frame.calibration_valid !== true) {
      items.push({ frameId: frame.id, step: "calibrate",
        route: { section: "wall", id: frame.id, tab: "position" } });
    }
  }
  return items;
}
