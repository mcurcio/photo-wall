import { frameStatusIn, piSilent } from "./domain/frame-status.ts";
import { ageAt, formatAge, playerLiveness } from "./health.js";

/** Plain operator recovery wording for the photo app's readiness failure codes. Home draws these, so
 * they name the photo app and the Pi, never the Player (decision 0036, Copy). */
const RECOVERY = Object.freeze({
  capacity:
    "The photo app could not prepare this assignment within what its Pi can do at once. Reduce concurrent video or effect work, or use lighter media.",
  decode:
    "The photo app could not decode this assignment. Check that the media is supported, or choose another item.",
  download:
    "The photo app could not secure this assignment. Check the Pi's storage and its network path to Central, then check Central media delivery.",
  integrity:
    "The photo app found that its cached copy did not pass the integrity check. Check the Pi's cache and Central media delivery so it can reacquire a valid copy.",
});

const UNKNOWN_RECOVERY =
  "The photo app reported an unrecognized readiness failure. Check the Pi's and Central's diagnostics.";

/**
 * Convert a protocol failure code to bounded, plain-language recovery guidance.
 * Unknown codes deliberately share one safe fallback and are never echoed.
 */
export function readinessRecoveryText(code) {
  return typeof code === "string" ? RECOVERY[code] ?? UNKNOWN_RECOVERY : UNKNOWN_RECOVERY;
}

/**
 * Current readiness guidance for a Frame, deduplicated across assignments.
 * Central already projects only current assignment failures. Silence takes
 * precedence because an old readiness report cannot diagnose a silent Player.
 */
export function readinessRecoveryForFrame(snapshot, frameId) {
  return readinessReportForFrame(snapshot, frameId)?.messages ?? [];
}

/** Current report wording plus its safe age, measured on Central's snapshot clock. */
export function readinessReportForFrame(snapshot, frameId) {
  // Central can't hear the Frame's Pi (its served status, domain/frame-status.ts): what the Pi
  // reported before is no guide to it now.
  const status = frameStatusIn(snapshot, frameId);
  if (piSilent(status)) return null;
  // Setup is not status: for a Frame still in setup the status says nothing of its Pi, so the
  // Pi's liveness as Central records it decides (health.js `playerLiveness`).
  if (status?.setup != null && !heardFrame(snapshot, frameId)) return null;
  const diagnostics = (snapshot?.readinessDiagnostics ?? []).filter(
    (diagnostic) => diagnostic.frame_id === frameId,
  );
  if (diagnostics.length === 0) return null;
  const codes = new Set(diagnostics.map((diagnostic) => diagnostic.failure_code));
  const receivedAt = Math.max(...diagnostics.map((diagnostic) => diagnostic.received_at));
  const age = ageAt(snapshot?.readAt, receivedAt);
  return {
    messages: [...codes].map(readinessRecoveryText),
    age: Number.isFinite(age) && age >= 0 ? formatAge(age) : null,
  };
}

/** Whether the Frame's Pi is live by Central's record, within its silence limit (no Pi: not live). */
function heardFrame(snapshot, frameId) {
  const playerId = (snapshot?.inventory?.frames ?? []).find((frame) => frame.id === frameId)?.player_id;
  return playerId != null && playerLiveness(snapshot, playerId)?.state === "heard";
}

/** Frames with a current accepted readiness failure, ordered by Frame id. */
export function readinessRecoveryFrames(snapshot) {
  const ids = new Set((snapshot?.readinessDiagnostics ?? []).map((entry) => entry.frame_id));
  return [...ids]
    .sort()
    .map((frameId) => ({ frameId, ...readinessReportForFrame(snapshot, frameId) }))
    .filter((entry) => entry.messages?.length > 0);
}
