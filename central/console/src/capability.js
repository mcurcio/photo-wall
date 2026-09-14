/**
 * Capability derivation (shared primitive #5) — design §7.6.
 *
 * A capability is DERIVED FROM A REAL WIRED PATH, never declared by a stored
 * boolean and never toggled by the operator. This makes the false-enabled
 * ("lying flag") state UNREPRESENTABLE at construction strength: a hardware
 * control cannot render unless a signal derived from a live wire says the path
 * exists.
 *
 *  - `display_command` (T2) would be derived from a live player session that has
 *    negotiated the command-protocol version — it cannot be true unless a real
 *    player on the wire can actually receive the command.
 *  - `photometric_calibration` (T1) would be derived from the build whose
 *    `Calibration` model carries the photometric field (model/version
 *    introspection) — never an operator toggle.
 *
 * **T0 builds ONLY the default-closed branch.** No signal is derivable today, so
 * this ALWAYS returns "absent" and every gated hardware area is correctly closed
 * with zero backend. This function is the SINGLE point a real T1/T2 signal is
 * later wired in; there is deliberately no branch here that reads a bare stored
 * flag as true, so no future edit can accidentally admit the lying-flag state
 * without wiring an actual derivation.
 *
 * @typedef {"absent"|"derived-true"} CapabilityState
 * @param {"photometric_calibration"|"display_command"} name  the capability to derive
 * @param {object} snapshot  the Plane A snapshot (the wire a real signal is read from)
 * @returns {CapabilityState}
 */
export function derive(name, snapshot) {
  // T0: no wired path is derivable from the snapshot, for any capability.
  // The default-closed branch is the ONLY branch. When T1/T2 lands, a real
  // derivation from `snapshot` (a negotiated session / a model-field probe)
  // is added here — never a bare boolean read.
  void name;
  void snapshot;
  return "absent";
}
