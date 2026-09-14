import React from "react";

/**
 * Hardware-control area gated on a derived capability (shared primitive #5) —
 * design §7.6.
 *
 * The live control (`children`) renders ONLY when the capability was DERIVED
 * from a real wired path (`state === "derived-true"`). For every other state —
 * including the T0 default "absent" — the area renders an explicit, honest
 * "not yet available" notice and NO enabled control. Because the control is the
 * children of this component, a control simply does not exist in the DOM until a
 * real derivation flips the gate: the lying-flag state (an enabled control with
 * no wire behind it) cannot be reached by mislabelling a stored boolean, only by
 * making `derive` genuinely return "derived-true" — which T0 never does.
 *
 * @param {{state: import("./capability.js").CapabilityState, children: React.ReactNode}} props
 */
export function GatedArea({ state, children }) {
  if (state === "derived-true") {
    return <>{children}</>;
  }
  return (
    <p className="gated-area gated-area--unavailable">
      Requires the display-control capability — not yet available.
    </p>
  );
}
