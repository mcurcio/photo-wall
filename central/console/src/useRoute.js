import { useCallback, useEffect, useMemo, useState } from "react";

import { formatRoute, parseRoute } from "./routes.js";

/**
 * The current hash route and the one way to change it (flow design §6).
 *
 * This hook is the ONLY code that writes `location.hash`; links in the page change it
 * natively. It listens to `hashchange`, so browser Back and Forward, a typed URL and a
 * link all arrive the same way.
 *
 * `navigate(route)` pushes a history entry; `navigate(route, {replace: true})` replaces
 * the current one (landing, plain selection on the Wall, and flow steps later), then
 * announces the change with a `hashchange` event, since `history.replaceState` sends none.
 * Navigating to the route already shown does nothing.
 *
 * @returns {{route: import("./routes.js").Route|null,
 *            navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void}}
 */
export function useRoute() {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    onChange(); // a change between the first render and this effect
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const route = useMemo(() => parseRoute(hash), [hash]);

  const navigate = useCallback((next, { replace = false } = {}) => {
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

  return { route, navigate };
}
