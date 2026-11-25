/** The everyday actions' words (decision 0036, slice 3): why Central refused one, why a Frame
 * stayed when the rest of its Wall moved, what each answer comes to, what a requested action says
 * while its Pi has not shown it, and Show now's ends. Pure, no markup; each vocabulary an
 * exhaustive `Record` over the generated contract, so a code Central adds and this module does not
 * word fails the typecheck. It runs under Node as it is (a test imports it), so it holds only
 * erasable TypeScript and names its imports' files. */
import type {
  AskedAction, EverydayRefusal, LastShowing, NeverShowAnswer, PauseAnswer, PauseRefusal, PauseStayed, ShowNowRefusal,
  SkipAnswer, SkipKind, Stayed, StayReason,
} from "../generated/contract.ts";
import { instantWords, whoWords } from "./frame-status.ts";

/** Why Central did not take a Skip, Previous, Never show, Pause or Carry on request, in plain words. */
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

/** Why Central did not start a Show now on the chosen Frames, in plain words. */
export const SHOW_NOW_REFUSAL_WORDS: Record<ShowNowRefusal, string> = {
  "no-frames": "Choose at least one Frame.",
  "frame-not-in-scene": "A chosen Frame is not one this Scene shows on.",
  "no-night-off": "There is no Night off tonight to end it at.",
  "ends-passed": "Its end time has already passed.",
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

/** A wall's two buttons: their words, and their accessible names, which say they move every Frame. */
export const WALL_ACTION_WORDS: Record<SkipKind, { label: string; name: (wall: string) => string }> = {
  next: { label: "Skip", name: (wall) => `Skip every Frame on ${wall}` },
  previous: { label: "Previous", name: (wall) => `Previous on every Frame on ${wall}` },
};

/** What a tile says while an action asked of it waits for its Pi to show it (an `AckBadge`). */
export const ASKED_WORDS: Record<AskedAction, string> = {
  skip: "Skip requested",
  previous: "Previous requested",
  "never-show": "Change requested",
  pause: "Pause requested",
  "carry-on": "Carry on requested",
  "back-to-normal": "Back to normal requested",
};

/** What did not happen when an action is late, as a card's sentence after the Frame's name. */
const LATE_WORDS: Record<AskedAction, string> = {
  skip: "didn't change. Its Pi hasn't reported the new photo.",
  previous: "didn't change. Its Pi hasn't reported the earlier photo.",
  "never-show": "didn't change. Its Pi hasn't reported a new photo.",
  pause: "isn't confirmed paused. Its Pi hasn't reported holding its photo.",
  "carry-on": "didn't carry on. Its Pi hasn't reported the schedule's photo.",
  "back-to-normal": "didn't go back to normal. Its Pi hasn't reported the schedule's photo.",
};

/** A late action on a Frame in one sentence: "Hall didn't change. Its Pi hasn't reported the new photo." */
export function lateWords(action: AskedAction, frame: string): string {
  return `${frame} ${LATE_WORDS[action]}`;
}

/** Show now's ends, as the sheet offers them (settings #84; tonight's Night off; until stopped). */
export type ShowNowEndKind = "in" | "night-off" | "stopped";
export const SHOW_NOW_END_WORDS: Record<ShowNowEndKind, string> = {
  in: "In 1 hour",
  "night-off": "At tonight's Night off",
  stopped: "Until I stop it",
};

/** The length Show now's "In 1 hour" sends (setting #84's default: the sheet's own). */
export const SHOW_NOW_SECONDS = 3600;

type Names = Readonly<Record<string, string>>;

const named = (names: Names, frameId: string) => names[frameId] ?? frameId;
const frames = (count: number) => `${count} ${count === 1 ? "Frame" : "Frames"}`;

/** Each Frame that stayed, one clause each: "Hall stayed: its photo is hand-placed." */
function stayedWords(stayed: readonly Stayed[] | undefined, names: Names): string[] {
  return (stayed ?? []).map((stay) => `${named(names, stay.frame_id)} stayed: ${STAY_WORDS[stay.reason]}.`);
}

/** A Skip's or Previous's answer: "Skipped on 4 Frames. Hall stayed: its photo is hand-placed." */
export function skipSaid(answer: SkipAnswer, names: Names): string {
  const moved = answer.moved ?? [];
  const verb = answer.kind === "previous" ? "Stepped back" : "Skipped";
  const lead = moved.length === 1 ? `${verb} on ${named(names, moved[0].frame_id)}.` : `${verb} on ${frames(moved.length)}.`;
  return [lead, ...stayedWords(answer.stayed, names)].join(" ");
}

/** Nothing moved (409 "nothing-moved"): which Frames stayed, and why. */
export function nothingMovedSaid(stayed: readonly Stayed[], names: Names): string {
  return [EVERYDAY_REFUSAL_WORDS["nothing-moved"], ...stayedWords(stayed, names)].join(" ");
}

/** The toast as Never show is pressed, before Central answers. */
export const NEVER_SHOW_SAID = "Won't show again.";

/** Where a photo listed just now shows one last time: "It shows once more on Hall, where it's already downloaded."
 * Each Frame is named once, however many of its turns Central names. */
function lastShowingWords(last: readonly LastShowing[], names: Names): string[] {
  if (last.length === 0) return [];
  const where = [...new Set(last.map((showing) => showing.frame_id))].map((frameId) => named(names, frameId)).join(", ");
  return [`It shows once more on ${where}, where it's already downloaded.`];
}

/** Never show's answer, as its toast says it: listed, where it shows once more, and who keeps it to its end. */
export function neverShowSaid(answer: NeverShowAnswer, names: Names): string {
  const kept = (answer.stayed ?? []).map((stay) =>
    `It stays on ${named(names, stay.frame_id)} to the end of its turn: ${STAY_WORDS[stay.reason]}.`);
  return [NEVER_SHOW_SAID, ...lastShowingWords(answer.last_showings ?? [], names), ...kept].join(" ");
}

/** Each Frame not paused, one clause each: "Hall wasn't paused: it's playing a clip, not a still photo." */
function notPausedWords(stayed: readonly PauseStayed[] | undefined, names: Names): string[] {
  return (stayed ?? []).map((stay) => `${named(names, stay.frame_id)} wasn't paused: ${PAUSE_STAY_WORDS[stay.reason]}.`);
}

/** Pause's answer: "Hall paused until 22:04." with each Frame that was not. */
export function pauseSaid(answer: PauseAnswer, names: Names, now: number | null): string {
  const paused = answer.paused ?? [];
  const who = paused.length === 1 ? named(names, paused[0].frame_id) : frames(paused.length);
  return [`${who} paused until ${instantWords(answer.ends_at, now)}.`, ...notPausedWords(answer.stayed, names)].join(" ");
}

/** Nothing paused (409 "nothing-paused"): which Frames, and why. */
export function nothingPausedSaid(stayed: readonly PauseStayed[], names: Names): string {
  return [EVERYDAY_REFUSAL_WORDS["nothing-paused"], ...notPausedWords(stayed, names)].join(" ");
}

/**
 * Why a press was not taken, in words: Central's own code worded, else that Central did not
 * answer or did not take it; a code is never shown.
 */
export function notTakenSaid(error: string, lead = "Not changed"): string {
  if (error in EVERYDAY_REFUSAL_WORDS) return `${lead}: ${EVERYDAY_REFUSAL_WORDS[error as EverydayRefusal]}`;
  if (error in SHOW_NOW_REFUSAL_WORDS) return `${lead}: ${SHOW_NOW_REFUSAL_WORDS[error as ShowNowRefusal]}`;
  if (error === "unreachable") return `${lead}: Central did not answer. Press it again.`;
  if (error === "lock_busy") return `${lead}: Central was busy. Press it again.`;
  return `${lead}: Central did not take it. Read the page again, then press it again.`;
}

/** The Never show list with nothing on it. */
export const NEVER_SHOW_EMPTY = "Nothing hidden from the wall. Never show a photo from its Frame's menu on Home.";

/** A listed photo, by its kind. */
export function photoWords(kind: "image" | "video"): string {
  return kind === "video" ? "A clip" : "A photo";
}

/** When a photo was listed, and by whom where that can be said: "21:04", "by Matt · 21:04". */
export function listedWords(entry: { by: string; listed_at: number }, now: number | null): string {
  const when = instantWords(entry.listed_at, now);
  const who = whoWords(entry.by);
  return who === null ? when : `by ${who} · ${when}`;
}

/** Show again's answer, with its Undo. */
export const SHOWN_AGAIN_SAID = "It shows again.";
