import { FRAME_ID_PATTERN } from "./frameIds.js";

/**
 * The console's hash routes (flow design §6): pure parsing and formatting, no React.
 *
 * A Route names one sidebar section and, for some sections, an instance within it:
 *
 *   #/now                          {section: "now"}
 *   #/now/show/<step>              {section: "now", flow: "show", step}
 *   #/scenes/new/<step>            {section: "scenes", flow: "new", step}
 *   #/scenes/new/<step>?target=<frame-id>  new Scene with an initial Frame selection
 *   #/scenes/<id>/edit/<step>      {section: "scenes", id, flow: "edit", step}
 *   #/sources/new/<step>           {section: "sources", flow: "new", step}
 *   #/schedule/new/<step>          {section: "schedule", flow: "new", step}
 *   #/schedule/<id>/edit/<step>    {section: "schedule", id, flow: "edit", step}
 *   #/wall/layout                  {section: "wall", mode: "layout"} Edit layout (console DDD §61)
 *   #/wall/frames/<id>/<facet>     {section: "wall", id, facet}
 *   #/wall/frames/<id>             {section: "wall", id, facet: "status"}: a Frame route with
 *                                  no facet opens Status (§61); never formatted
 *   #/players/<device-id>          {section: "players", id} one Player's page
 *   #/releases/update/<tag>        {section: "releases", flow: "update", id} Update the wall
 *   #/releases/update/<tag>/try/<player-id>  … with the operator's tried Player (Part E §25a)
 *   #/releases/update/<tag>[/try/<player-id>]/skip/<player-id>[/<player-id>…]  … and the
 *                                  Players the operator skipped in Keep's plan (Part E §25a)
 *   #/<section>                    {section} for every section
 *   #/equipment                    {section: "players"}: the retired Equipment page's
 *                                  bookmark (console DDD §9); never formatted
 *   #/wall/frames/<id>/commissioning  {section: "wall", id, facet: "calibration"}: the
 *                                  renamed facet's old bookmark (console DDD §19); never
 *                                  formatted
 *   #/wall/frames/<id>/nowshowing  {section: "wall", id, facet: "status"}: the Now-showing
 *                                  facet's old bookmark (console DDD §61); never formatted
 *
 * Steps are the flows' own ids (beads 2-5); any non-empty segment parses. Facets are
 * the Inspector's keys. Ids and steps are URI-encoded, so an id may hold any text.
 * Anything else parses to null, which the shell replaces with the landing route.
 *
 * `formatRoute` is the inverse: for every Route `r` it accepts,
 * `sameRoute(parseRoute(formatRoute(r)), r)` holds, and it throws for a value that is
 * not a Route, so a caller's mistake cannot write an unparseable hash.
 *
 * @typedef {"now"|"scenes"|"schedule"|"sources"|"wall"|"players"|"releases"|"attention"} Section
 * @typedef {"new"|"edit"|"show"|"update"} Flow
 * @typedef {"status"|"binding"|"calibration"} Facet
 * @typedef {"layout"} Mode
 * @typedef {{section: Section, id?: string, flow?: Flow, step?: string, facet?: Facet,
 *            mode?: Mode, initialTarget?: string, tried?: string, skipped?: string[]}} Route
 *   `tried` is the Player an Update the wall journey tries the release on, `skipped` the Players
 *   it must never reboot, in the order the operator skipped them, none repeated (its URL holds
 *   only the operator's choices, never progress: Part E §25a). An empty `skipped` is the same
 *   route as none.
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

/** Every section, in sidebar order (console DDD §48: Wall, Show, Fleet, Needs attention). */
export const SECTIONS = Object.freeze([
  "wall",
  "now",
  "scenes",
  "schedule",
  "sources",
  "players",
  "releases",
  "attention",
]);

// Old section names that parse to a current one, so their bookmarks keep working.
const ALIASES = Object.freeze({ equipment: "players" });

/** The Inspector's facet keys, in its tab order (Inspector.jsx FACETS). */
export const FACETS = Object.freeze(["status", "binding", "calibration"]);

/** The facet a Frame opens on when nothing names one (console DDD §61, G3). */
export const DEFAULT_FACET = "status";

// Old facet names that parse to a current one, so their bookmarks keep working. No other
// facet segment ever shipped, so there is no other alias.
export const FACET_ALIASES = Object.freeze({ nowshowing: "status", commissioning: "calibration" });

// The Wall's modes (`#/wall/<mode>`): Edit layout only.
const WALL_MODES = new Set(["layout"]);

// The sections whose flow starts at `#/<section>/new/<step>`.
const NEW_FLOWS = new Set(["scenes", "sources", "schedule"]);

const KEYS = ["section", "id", "flow", "step", "facet", "mode", "initialTarget", "tried", "skipped"];

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
  const rawPath = hash.startsWith("#") ? hash.slice(1) : hash;
  const queryAt = rawPath.indexOf("?");
  const path = queryAt < 0 ? rawPath : rawPath.slice(0, queryAt);
  const query = queryAt < 0 ? "" : rawPath.slice(queryAt + 1);
  if (queryAt >= 0 && query === "") return null;
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
  const [named, ...rest] = parts;
  if (Object.hasOwn(ALIASES, named)) {
    return rest.length === 0 && query === "" ? { section: ALIASES[named] } : null;
  }
  const section = named;
  if (!SECTIONS.includes(section)) {
    return null;
  }
  if (rest.length === 0) {
    if (query !== "") return null;
    return { section };
  }
  if (query !== "" && !(section === "scenes" && rest.length === 2 && rest[0] === "new")) {
    return null;
  }
  if (section === "wall" && rest.length === 1 && WALL_MODES.has(rest[0])) {
    return { section, mode: rest[0] };
  }
  if (section === "wall" && rest.length === 2 && rest[0] === "frames") {
    return { section, id: rest[1], facet: DEFAULT_FACET };
  }
  if (section === "wall" && rest.length === 3 && rest[0] === "frames") {
    const facet = Object.hasOwn(FACET_ALIASES, rest[2]) ? FACET_ALIASES[rest[2]] : rest[2];
    if (FACETS.includes(facet)) return { section, id: rest[1], facet };
  }
  if (section === "players" && rest.length === 1) {
    return { section, id: rest[0] };
  }
  if (section === "releases" && rest[0] === "update" && rest.length >= 2) {
    return updateRoute(rest[1], rest.slice(2));
  }
  if (section === "now" && rest.length === 2 && rest[0] === "show") {
    return { section, flow: "show", step: rest[1] };
  }
  if (NEW_FLOWS.has(section) && rest.length === 2 && rest[0] === "new") {
    if (query === "") return { section, flow: "new", step: rest[1] };
    const params = new URLSearchParams(query);
    const targets = params.getAll("target");
    if ([...params].length !== 1 || targets.length !== 1 || !FRAME_ID_PATTERN.test(targets[0])) {
      return null;
    }
    return { section, flow: "new", step: rest[1], initialTarget: targets[0] };
  }
  if ((section === "scenes" || section === "sources" || section === "schedule") && rest.length === 3 && rest[1] === "edit") {
    return { section, id: rest[0], flow: "edit", step: rest[2] };
  }
  return null;
}

/** An Update the wall route from its tag and the segments after it, or null. */
function updateRoute(id, tail) {
  const route = { section: "releases", flow: "update", id };
  let rest = tail;
  if (rest[0] === "try" && rest.length >= 2) {
    route.tried = rest[1];
    rest = rest.slice(2);
  }
  if (rest.length === 0) return route;
  const skipped = rest.slice(1);
  if (rest[0] !== "skip" || skipped.length === 0 || new Set(skipped).size !== skipped.length) return null;
  return { ...route, skipped };
}

/**
 * Format a Route as a location hash ("#/…"). Throws if `route` is not a Route.
 *
 * @param {Route} route
 * @returns {string}
 */
export function formatRoute(route) {
  const { section, id, flow, step, facet, mode, initialTarget, tried, skipped } = route ?? {};
  if (initialTarget !== undefined &&
      (section !== "scenes" || flow !== "new" || facet !== undefined ||
        !FRAME_ID_PATTERN.test(initialTarget))) {
    throw new Error(`not a console route: ${JSON.stringify(route)}`);
  }
  let parts;
  if (mode !== undefined) {
    parts = [section, mode];
  } else if (facet !== undefined) {
    parts = [section, "frames", id, facet];
  } else if (flow === "edit") {
    parts = [section, id, "edit", step];
  } else if (flow === "update") {
    parts = [section, "update", id, ...(tried === undefined ? [] : ["try", tried]),
      ...(Array.isArray(skipped) && skipped.length > 0 ? ["skip", ...skipped] : [])];
  } else if (flow !== undefined) {
    parts = [section, flow, step];
  } else if (id !== undefined) {
    parts = [section, id];
  } else {
    parts = [section];
  }
  const path =
    "#/" + parts.map((part) => encodeURIComponent(typeof part === "string" ? part : "")).join("/");
  const hash = initialTarget === undefined
    ? path
    : `${path}?target=${encodeURIComponent(initialTarget)}`;
  const parsed = parseRoute(hash);
  if (parsed === null || !sameRoute(parsed, route)) {
    throw new Error(`not a console route: ${JSON.stringify(route)}`);
  }
  return hash;
}

/** A new Scene route whose fresh draft starts with this Frame explicitly selected. */
export function sceneCreationRoute(frameId) {
  if (typeof frameId !== "string" || !FRAME_ID_PATTERN.test(frameId)) {
    throw new Error(`not a Frame identifier: ${String(frameId)}`);
  }
  return { section: "scenes", flow: "new", step: "kind", initialTarget: frameId };
}

/**
 * Where an unknown route lands: always the Wall (console DDD §48, G3). With no Frames the
 * Wall's own face is the first step (its Guidance banner), so the landing never depends on
 * state.
 *
 * @returns {Route}
 */
export function landingRoute() {
  return { section: "wall" };
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
  return extra.length === 0 && KEYS.every((key) => sameValue(a[key], b[key]));
}

// A list key (`skipped`) compares element by element, and an empty list is the same as none.
function sameValue(x, y) {
  if (!Array.isArray(x) && !Array.isArray(y)) return x === y;
  const [xs, ys] = [x ?? [], y ?? []];
  return Array.isArray(xs) && Array.isArray(ys) && xs.length === ys.length && xs.every((item, at) => item === ys[at]);
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
