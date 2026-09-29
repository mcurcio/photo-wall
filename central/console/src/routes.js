/**
 * The console's hash routes (flow design §6): pure parsing and formatting, no React.
 *
 * A Route names one sidebar section and, for some sections, an instance within it:
 *
 *   #/now                          {section: "now"}
 *   #/now/show/<step>              {section: "now", flow: "show", step}
 *   #/scenes/new/<step>            {section: "scenes", flow: "new", step}
 *   #/scenes/<id>/edit/<step>      {section: "scenes", id, flow: "edit", step}
 *   #/sources/new/<step>           {section: "sources", flow: "new", step}
 *   #/schedule/new/<step>          {section: "schedule", flow: "new", step}
 *   #/wall/frames/<id>/<facet>     {section: "wall", id, facet}
 *   #/<section>                    {section} for every section
 *
 * Steps are the flows' own ids (beads 2-5); any non-empty segment parses. Facets are
 * the Inspector's keys. Ids and steps are URI-encoded, so an id may hold any text.
 * Anything else parses to null, which the shell replaces with the landing route.
 *
 * `formatRoute` is the inverse: for every Route `r` it accepts,
 * `sameRoute(parseRoute(formatRoute(r)), r)` holds, and it throws for a value that is
 * not a Route, so a caller's mistake cannot write an unparseable hash.
 *
 * @typedef {"now"|"scenes"|"schedule"|"sources"|"wall"|"equipment"|"attention"} Section
 * @typedef {"new"|"edit"|"show"} Flow
 * @typedef {"commissioning"|"binding"|"nowshowing"} Facet
 * @typedef {{section: Section, id?: string, flow?: Flow, step?: string, facet?: Facet}} Route
 */

/**
 * The API identifier rule: contracts/models.py `IDENTIFIER_PATTERN` (Scene, Program,
 * Source and Frame ids are path parameters under it). A pytest pins the two equal, so
 * there is one rule; authoring.js reads it from here.
 */
export const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

/**
 * How a notice names the record a route's id points at: "<noun> <id>" when the id is one
 * Central could store (`IDENTIFIER_PATTERN`), otherwise "an unknown <noun>". A typed URL
 * may hold any text, and a notice never echoes it. `start` capitalises it to open a
 * sentence.
 *
 * @param {string} noun "Scene", "Frame"
 * @param {string} id
 * @param {{start?: boolean}} [options]
 * @returns {string}
 */
export function routeIdName(noun, id, { start = false } = {}) {
  if (IDENTIFIER_PATTERN.test(id)) {
    return `${noun} ${id}`;
  }
  return `${start ? "An" : "an"} unknown ${noun}`;
}

/** Every section, in sidebar order. */
export const SECTIONS = Object.freeze([
  "now",
  "scenes",
  "schedule",
  "sources",
  "wall",
  "equipment",
  "attention",
]);

/** The Inspector's facet keys (Inspector.jsx FACETS). */
export const FACETS = Object.freeze(["commissioning", "binding", "nowshowing"]);

// The sections whose flow starts at `#/<section>/new/<step>`.
const NEW_FLOWS = new Set(["scenes", "sources", "schedule"]);

const KEYS = ["section", "id", "flow", "step", "facet"];

/**
 * Parse a location hash (with or without its leading "#") into a Route, or null.
 *
 * @param {string} hash
 * @returns {Route|null}
 */
export function parseRoute(hash) {
  if (typeof hash !== "string") {
    return null;
  }
  const path = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!path.startsWith("/")) {
    return null;
  }
  let parts;
  try {
    // Split before decoding, so an encoded "/" stays inside its segment.
    parts = path.slice(1).split("/").map(decodeURIComponent);
  } catch {
    return null; // a malformed escape
  }
  if (parts.some((part) => part === "")) {
    return null;
  }
  const [section, ...rest] = parts;
  if (!SECTIONS.includes(section)) {
    return null;
  }
  if (rest.length === 0) {
    return { section };
  }
  if (section === "wall" && rest.length === 3 && rest[0] === "frames" && FACETS.includes(rest[2])) {
    return { section, id: rest[1], facet: rest[2] };
  }
  if (section === "now" && rest.length === 2 && rest[0] === "show") {
    return { section, flow: "show", step: rest[1] };
  }
  if (NEW_FLOWS.has(section) && rest.length === 2 && rest[0] === "new") {
    return { section, flow: "new", step: rest[1] };
  }
  if ((section === "scenes" || section === "sources") && rest.length === 3 && rest[1] === "edit") {
    return { section, id: rest[0], flow: "edit", step: rest[2] };
  }
  return null;
}

/**
 * Format a Route as a location hash ("#/…"). Throws if `route` is not a Route.
 *
 * @param {Route} route
 * @returns {string}
 */
export function formatRoute(route) {
  const { section, id, flow, step, facet } = route ?? {};
  let parts;
  if (facet !== undefined) {
    parts = [section, "frames", id, facet];
  } else if (flow === "edit") {
    parts = [section, id, "edit", step];
  } else if (flow !== undefined) {
    parts = [section, flow, step];
  } else {
    parts = [section];
  }
  const hash =
    "#/" + parts.map((part) => encodeURIComponent(typeof part === "string" ? part : "")).join("/");
  const parsed = parseRoute(hash);
  if (parsed === null || !sameRoute(parsed, route)) {
    throw new Error(`not a console route: ${JSON.stringify(route)}`);
  }
  return hash;
}

/**
 * Where an unknown route lands (§6, Question 4): the Wall while no frame exists (its
 * Guidance banner is there), otherwise Now showing.
 *
 * @param {number} frameCount
 * @returns {Route}
 */
export function landingRoute(frameCount) {
  return { section: frameCount === 0 ? "wall" : "now" };
}

/**
 * Whether two Routes name the same place (absent keys and undefined are the same).
 *
 * @param {Route|null} a
 * @param {Route|null} b
 * @returns {boolean}
 */
export function sameRoute(a, b) {
  if (a == null || b == null) {
    return a == b;
  }
  const extra = Object.keys(b).filter((key) => !KEYS.includes(key) && b[key] !== undefined);
  return extra.length === 0 && KEYS.every((key) => a[key] === b[key]);
}

/**
 * A primary click with no modifier: a link followed in this tab, not opened in another.
 * Only such a click should move this tab's focus or prepare its next page.
 *
 * @param {{button: number, metaKey: boolean, ctrlKey: boolean, shiftKey: boolean,
 *          altKey: boolean}} event
 * @returns {boolean}
 */
export function isPlainClick(event) {
  return (
    event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey
  );
}
