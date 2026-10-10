import { useCallback, useEffect, useMemo, useState } from "react";

import { formatRoute, parseRoute } from "./routes.js";

/**
 * The current hash route and the one way to change it (flow design §6).
 *
 * This hook is the ONLY code that writes `location.hash`; links in the page change it
 * natively. It listens to `hashchange`, so browser Back and Forward, a typed URL and a
 * link all arrive the same way, and to `popstate`, which the browser fires in the same
 * task that moves the location (a traversal such as a flow's `history.back()`, or a
 * fragment navigation), while that move's `hashchange` is queued as a later task. So
 * the rendered route never waits behind a render already due: a render after the
 * location moved shows the new route, never the one the browser has left.
 *
 * `navigate(route)` pushes a history entry; `navigate(route, {replace: true})` replaces
 * the current one (landing, plain selection on the Wall, and flow steps later), then
 * announces the change with a `hashchange` event, since `history.replaceState` sends none.
 * Navigating to the route already shown does nothing. A hash that parses but is not its
 * route's canonical form (`#/wall/frames/<id>`, which parses to its Overview tab) is
 * replaced by it.
 *
 * `navigate(route, {replace: true, ifUnknown: true})` (the landing route) goes only if
 * the hash names no route WHEN IT RUNS. The caller decides from a rendered `route`, which
 * lags the hash: a link or typed URL sets the hash at once but its `hashchange` arrives
 * later, so a landing decided from the stale `null` would otherwise overwrite a section
 * the operator had just chosen.
 *
 * @typedef {{replace?: boolean, ifUnknown?: boolean}} NavigateOptions
 * @returns {{route: import("./routes.js").Route|null,
 *            navigate: (route: import("./routes.js").Route, options?: NavigateOptions) => void}}
 */
export function useRoute() {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    window.addEventListener("popstate", onChange);
    onChange(); // a change between the first render and this effect
    return () => {
      window.removeEventListener("hashchange", onChange);
      window.removeEventListener("popstate", onChange);
    };
  }, []);

  const route = useMemo(() => parseRoute(hash), [hash]);

  const navigate = useCallback((next, { replace = false, ifUnknown = false } = {}) => {
    if (ifUnknown && parseRoute(window.location.hash) !== null) {
      return;
    }
    const target = formatRoute(next);
    if (target === window.location.hash) {
      return;
    }
    if (replace) {
      const { pathname, search } = window.location;
      window.history.replaceState(window.history.state, "", pathname + search + target);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    } else {
      window.location.hash = target;
    }
  }, []);

  // A hash that parses to a route it is not the canonical form of (a Frame route with no tab,
  // `#/wall/frames/<id>`) is replaced by that form, so the location names the page shown.
  useEffect(() => {
    if (route !== null && formatRoute(route) !== hash) {
      navigate(route, { replace: true });
    }
  }, [route, hash, navigate]);

  return { route, navigate };
}
