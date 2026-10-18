import type { Calibration, FrameLighting, TrialBegin, TrialEdit } from "./generated/contract";

/*
 * The bodies a Frame's live session hands Central with a draft, as Central's schemas type them
 * (src/generated/contract.ts): liveAdjustment.js builds every one here, so a field Central renames
 * or retypes fails the typecheck instead of the session.
 */

/** The draft a session shows: its fit with its base revision, and its whole lighting. */
export type Draft = Pick<TrialEdit, "calibration" | "lighting">;

/** The draft as Central takes it. */
export function draftBody(calibration: Calibration, lighting: FrameLighting): Draft {
  return { calibration, lighting };
}

/** Begin a session: at the draft, or (with none) at the saved calibration and lighting. */
export function beginBody(draft: Draft | undefined): TrialBegin {
  return draft === undefined ? {} : { calibration: draft.calibration, lighting: draft.lighting };
}
