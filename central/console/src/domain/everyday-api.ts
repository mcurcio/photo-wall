import { apiWrite } from "../apiWrite.js";
import type {
  BackToNormalAnswer, CarryOnAnswer, NeverShowAnswer, NeverShowListView, PauseAnswer, SkipAnswer, SkipKind,
} from "../generated/contract";
import { CONSTANTS } from "../generated/contract";

/* The everyday actions' requests (decision 0036, slice 3; central/everyday_routes.py and, for Back
 * to normal, central/app.py; the shapes central/everyday/views.py's, in the generated contract).
 * Each answers `{ok: true, answer}` or `{ok: false, error, stayed?}`: `error` is Central's code
 * (domain/everyday-words.ts words it), `unreachable` for a lost request, `http_<status>` for an
 * answer with no code; `stayed` is what a 409 "nothing-moved" or "nothing-paused" names. Every
 * address is the contract's, never typed here; a `requestId` is fresh per press, so a doubled
 * press is one action. */

/** An everyday action's answer: Central's answer, or its code and who stayed. */
export type Answered<T> = { ok: true; answer: T } | { ok: false; error: string; stayed?: readonly object[] };

/** A path template with each `{name}` filled, encoded. */
function filled(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => encodeURIComponent(values[name]));
}

/** One request folded into `Answered`. */
async function asked<T>(path: string, options: { method: string; body?: unknown }): Promise<Answered<T>> {
  try {
    const result = await apiWrite(path, options);
    if (result.ok) return { ok: true, answer: result.data as T };
    const error = result.error ?? `http_${result.status}`;
    const stayed = (result.data as { stayed?: unknown } | null)?.stayed;
    return Array.isArray(stayed) ? { ok: false, error, stayed } : { ok: false, error };
  } catch {
    return { ok: false, error: "unreachable" };
  }
}

/** Skip or Previous on every Frame of a wall (the owner's S1, S2): each that can move goes to its
 * next (or previous) photo at one instant; the answer names who stayed and why. */
export function skipWall(surfaceId: string, kind: SkipKind, requestId: string): Promise<Answered<SkipAnswer>> {
  return asked(filled(CONSTANTS.WALL_SKIPS_PATH, { surface_id: surfaceId }),
    { method: "POST", body: { request_id: requestId, kind } });
}

/** Skip or Previous on one Frame alone (a Frame's own menu). */
export function skipFrame(frameId: string, kind: SkipKind, requestId: string): Promise<Answered<SkipAnswer>> {
  return asked(filled(CONSTANTS.FRAME_SKIPS_PATH, { frame_id: frameId }),
    { method: "POST", body: { request_id: requestId, kind } });
}

/** Never show a photo again: the photo a Frame shows (`FrameStatus.photo`), seen on `frameId`, so a
 * photo that turned over under the thumb answers 409 `changed` and nothing is listed. */
export function neverShow(assetId: string, frameId: string | null, requestId: string): Promise<Answered<NeverShowAnswer>> {
  return asked(CONSTANTS.NEVER_SHOW_PATH,
    { method: "POST", body: { request_id: requestId, asset_id: assetId, frame_id: frameId } });
}

/** Undo, and Show again: the photo goes back on the wall's lines. */
export function showAgain(assetId: string): Promise<Answered<{ asset_id: string; was_listed: boolean }>> {
  return asked(filled(CONSTANTS.SHOW_AGAIN_PATH, { asset_id: assetId }), { method: "DELETE" });
}

/** The Never show list (Scenes › Never show list): every listed photo, by whom and when. */
export function readNeverShowList(): Promise<Answered<NeverShowListView>> {
  return asked(CONSTANTS.NEVER_SHOW_PATH, { method: "GET" });
}

/** Pause Frames on the photo each shows, for Central's default length (setting #86). */
export function pause(frameIds: string[], requestId: string): Promise<Answered<PauseAnswer>> {
  return asked(CONSTANTS.PAUSES_PATH, { method: "POST", body: { request_id: requestId, frame_ids: frameIds } });
}

/** Carry on: the pause ends on every Frame of it, or on `frameId` alone. */
export function carryOn(pauseId: string, frameId: string | null = null): Promise<Answered<CarryOnAnswer>> {
  return asked(frameId === null ? filled(CONSTANTS.CARRY_ON_PATH, { pause_id: pauseId })
    : filled(CONSTANTS.CARRY_ON_FRAME_PATH, { pause_id: pauseId, frame_id: frameId }), { method: "DELETE" });
}

/** Back to normal: a takeover ends now, mid-photo, with its Scene's fade (the owner's S3). */
export function backToNormal(runId: string, requestId: string): Promise<Answered<BackToNormalAnswer>> {
  return asked(filled(CONSTANTS.RUN_END_PATH, { run_id: runId }), { method: "POST", body: { request_id: requestId } });
}

/** The requests above as one port (domain/everyday-actions.tsx `EverydayCommands`). */
export const everydayCommands = Object.freeze({
  skipWall, skipFrame, neverShow, showAgain, pause, carryOn, backToNormal,
});
