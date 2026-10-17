import type { PositionRestoreRefusal, TrialRefusal } from "../generated/contract";

/*
 * A Frame's live adjustment's refusals in the operator's words (liveAdjustment.js): every code the
 * trial routes answer (central/fleet/models.py `TrialRefusal`) and every reason a Position history
 * entry cannot be restored (`PositionRestoreRefusal`). Each table is a `Record` over Central's
 * generated union, so a code without words fails the typecheck. Central calls the session a
 * calibration trial; none of that wording reaches the operator. Pure: no React, no request.
 */

// Someone saved this Frame's calibration after the draft was made (Central's compare-and-set).
const OVERTAKEN =
  "Someone saved a different position for this Frame meanwhile. Press Revert to load it, then adjust again.";
const EDITOR_CLOSED = "The editor closed. It starts again in a moment; then restore.";

/** Every trial refusal, in words. */
export const TRIAL_REFUSAL_WORDS: Record<TrialRefusal, string> = {
  trial_already_active: "Another window is adjusting this Frame. Photo Wall tries again once it closes.",
  trial_baseline_revision_changed: OVERTAKEN,
  trial_unknown: EDITOR_CLOSED,
  trial_ended: EDITOR_CLOSED,
  trial_sequence_conflict: "Another window changed this Frame at the same moment.",
  trial_sequence_invalid: "Photo Wall refused this change: it was out of step with the editor. The next change fixes it.",
  trial_latest_not_presented: "The Pi has not presented the latest change yet. Wait a moment, then press Done again.",
  trial_frame_unbound: "Choose which Pi and HDMI output feed this Frame first (Hardware tab).",
  trial_display_unavailable: "This Frame's Pi is not connected to Photo Wall. Check that it is on.",
  trial_current_output_required:
    "The Pi has not reported this Display in the last few seconds. Check that the Display is on and plugged in.",
  trial_admitted_surface_required: "The Pi is still starting its picture. Wait a moment.",
  trial_surface_changed: "This Frame's Pi changed its picture while Photo Wall checked the change. It tries again in a moment.",
  unknown_entry: "That change is no longer in the history.",
  // A draft Central cannot read (a value outside what a Frame takes): the next edit fixes it.
  node_trial_operation_invalid: "Photo Wall refused this change: a value is outside what a Frame takes.",
  node_body_too_large: "Photo Wall refused this change: it is too large to send.",
  calibration_revision_conflict: OVERTAKEN,
  calibration_trial_baseline_changed: OVERTAKEN,
};

/** Every reason a Position history entry cannot be restored, in words. */
export const POSITION_RESTORE_WORDS: Record<PositionRestoreRefusal, string> = {
  other_frame: "That change was another Frame's.",
  made_for_other_profile: "Made for another screen size.",
  made_for_other_display: "Made on another display.",
};
