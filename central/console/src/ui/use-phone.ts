import { useEffect, useState } from "react";

import { PHONE_QUERY } from "../design/tokens";

/**
 * Whether the window is a phone's (below the one structural switch, design/tokens.ts): for the few
 * primitives whose structure, not only their look, changes there (a menu opens as a sheet). A
 * class is the way wherever a class can do it. False where there is no window to ask.
 */
export function usePhone(): boolean {
  const [phone, setPhone] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia(PHONE_QUERY).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia(PHONE_QUERY);
    const onChange = () => setPhone(query.matches);
    onChange();
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return phone;
}
