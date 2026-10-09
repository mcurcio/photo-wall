import { ageAt, formatAge, frameHealth } from "./health.js";

/** Plain operator recovery wording for Player readiness failure codes. */
const RECOVERY = Object.freeze({
  capacity:
    "The Player could not prepare this assignment within its available capacity. Reduce concurrent video or effect work, or use lighter media.",
  clock:
    "The Player reported clock uncertainty. Check its time synchronization and refresh Player status.",
  decode:
    "The Player could not decode this assignment. Check that the media is supported, or choose another item.",
  download:
    "The Player could not secure this assignment. Check Player storage and its network path to Central, then check Central media delivery.",
  integrity:
    "The Player found that its cached copy did not pass the integrity check. Check the Player cache and Central media delivery so it can reacquire a valid copy.",
  main_loop_late:
    "The Player is overloaded: its display loop is falling behind, so it cannot confirm this assignment. Use lighter media or fewer videos on this Player; it restarts itself if the loop stays stuck.",
});

const UNKNOWN_RECOVERY =
  "The Player reported an unrecognized readiness failure. Check Player and Central diagnostics.";

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
  const health = frameHealth(snapshot, frameId);
  if (health === null || health.cause === "liveness") return null;
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

/** Frames with a current accepted readiness failure, ordered by Frame id. */
export function readinessRecoveryFrames(snapshot) {
  const ids = new Set((snapshot?.readinessDiagnostics ?? []).map((entry) => entry.frame_id));
  return [...ids]
    .sort()
    .map((frameId) => ({ frameId, ...readinessReportForFrame(snapshot, frameId) }))
    .filter((entry) => entry.messages?.length > 0);
}
