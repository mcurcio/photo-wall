import React from "react";

/**
 * The console's inline icons (flow design §5): our own simple paths, drawn in the
 * current text colour. They are decorative; the control that holds one carries the
 * accessible name. Inline SVG needs no request and no `data:` URL, which the CSP
 * refuses.
 */

function Icon({ children }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      width="24"
      height="24"
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    >
      {children}
    </svg>
  );
}

/** Three horizontal bars: opens the navigation drawer. */
export function MenuIcon() {
  return (
    <Icon>
      <path d="M4 6h16M4 12h16M4 18h16" />
    </Icon>
  );
}

/** A cross: closes the navigation drawer. */
export function CloseIcon() {
  return (
    <Icon>
      <path d="M6 6l12 12M18 6L6 18" />
    </Icon>
  );
}
