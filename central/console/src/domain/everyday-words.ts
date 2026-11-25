/** The everyday actions' words (decision 0036, slice 3): why Central refused one, and why a Frame
 * stayed when the rest of its Wall moved. Pure, no markup; each an exhaustive `Record` over the
 * generated contract, so a code Central adds and this module does not word fails the typecheck. */
import type { EverydayRefusal, PauseRefusal, StayReason } from "../generated/contract.ts";

/** Why Central did not take a Skip, Previous or Never show request, in plain words. */
export const EVERYDAY_REFUSAL_WORDS: Record<EverydayRefusal, string> = {
  invalid_operator_skip: "Central could not read the request.",
  invalid_never_show: "Central could not read the request.",
  unknown_wall: "Central does not know this wall any more.",
  unknown_frame: "Central does not know this Frame any more.",
  unknown_photo: "This photo is no longer in the photo library.",
  "nothing-moved": "No Frame could move to another photo.",
  changed: "A new photo came up before you pressed, so nothing was hidden.",
  invalid_operator_pause: "Central could not read the request.",
  unknown_pause: "This pause has already ended.",
  "nothing-paused": "No Frame could be paused.",
  invalid_show_now: "Central could not read the request.",
};

/** Why a Frame was not paused, as the clause after its name ("Hall wasn't paused: …"). */
export const PAUSE_STAY_WORDS: Record<PauseRefusal, string> = {
  "not-set-up": "it isn't set up yet",
  "not-answering": "its Pi isn't answering",
  resting: "its display is off now",
  paused: "it's on hold already",
  "nothing-to-hold": "no photo is showing",
  video: "it's playing a clip, not a still photo",
};

/** Why a Frame stayed, as the clause after its name ("Hall stayed: …"). */
export const STAY_WORDS: Record<StayReason, string> = {
  "not-set-up": "it isn't set up yet",
  "not-answering": "its Pi isn't answering",
  resting: "it's resting",
  paused: "it's paused",
  "nothing-to-skip": "nothing is showing",
  black: "it's dark on purpose",
  "hand-placed": "its photo is hand-placed",
  "no-previous": "it has nothing earlier to show",
};
