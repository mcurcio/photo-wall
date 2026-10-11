import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { frameOf } from "../join.js";
import type { CropView } from "../patterns/crop-thumb";
import type { FitBoardState, FitBoardView, FitWarning } from "../patterns/fit-board";
import type { FitReason, FitSegment, FrameFitView } from "../patterns/frame-fit-row";
import type { ProblemAction } from "../patterns/problem-card";
import type { ShapeBin } from "../patterns/shape-map";
import { analyseFit, readFitSettings, saveFitSettings } from "../photoFitApi.js";
import { sourceName } from "../sourceNames.js";
import { countText, countWords, sinceTime } from "../timeWords.js";

/*
 * How a selection of photos fits the Frames (follow-ups 2026-10-11, slice C1): the analysis's
 * shapes (central/wall_plan/fit_analysis.py, their one home, mirrored field for field), the one
 * hook every host reads it through, and the one home of its words in the console: the fit
 * rule's reasons, ratios, the Planner's words for a Frame with nothing that fits, and the
 * sampled check. Owner, 2026-10-11: "the real solution should be more visual and allow me to
 * preview the content selections and transient Frame compatibility"; "this preview idea could
 * apply at a few different moments". Every moment builds its own question (a draft or a saved
 * selection, saved or draft Frames, a crop limit, draft settings) and reads the answer here;
 * none judges a photo itself.
 */

// --- The analysis, as Central answers it ------------------------------------------------------

/** Why the fit rule leaves a photo off a Frame (central/wall_plan/fit.py `FitRefusal`). */
export type FitRefusal = "no_video_here" | "beyond_crop_limit" | "below_frame_pixels" | "face_cut" | "interest_cut";
export type MediaKind = "image" | "video";

/** Every tuning value of the fit rule (central/wall_plan/fit_settings.py `FitSettings`). */
export interface FitSettings {
  crop_limit: number;
  face_padding: number;
  min_face_share: number;
  max_lost_interest_density: number;
  min_salience_spread: number;
  large_frame_inches: number;
  large_frame_video_short_side: number;
  window_steps: number;
}

export interface FitSelection {
  sources?: string[];
  drafts?: string[];
}

/** A Frame as it would be configured (its draft profile and Position). */
export interface FrameDraft {
  frame_id: string;
  profile: { width_px: number; height_px: number; diagonal_inches: number; video: boolean };
  calibration: object;
}

export interface FitAnalysisRequest {
  selection: FitSelection;
  frames?: string[] | null;
  frame_drafts?: FrameDraft[];
  crop_limit?: number | null;
  settings?: FitSettings | null;
  tier?: "shape" | "full";
  seed?: number;
}

export interface RatioBin { low: number; high: number; photos: number; videos: number }

export interface FitSample {
  asset_id: string;
  kind: MediaKind;
  width: number;
  height: number;
  crop: { offset: number };
  thumbnail: string;
}

export interface RefusalCount { reason: FitRefusal; photos: number; samples: FitSample[] }

export interface FrameFit {
  frame_id: string;
  aspect: number;
  output_px: [number, number] | null;
  display_px: [number, number] | null;
  draft: boolean;
  band: [number, number];
  photos: number;
  fit_by_shape: number;
  refused_by_shape: RefusalCount[];
  measured: number;
  nearest_crop_limit: number | null;
  checked: number;
  fit_checked: number;
  refused_by_content: RefusalCount[];
  lead: FitSample | null;
  fitting: FitSample[];
}

export interface FitAnalysis {
  tier: "shape" | "full";
  crop_limit: number;
  settings: FitSettings;
  photos: number;
  videos: number;
  waiting: number;
  left_out: number;
  reading: boolean;
  bins: RatioBin[];
  frames: FrameFit[];
  read_at: number;
}

/** Where a read stands: never read yet, an answer (with whether a newer one is on its way),
 * or a failure (with the last answer, if any, and when it was read). */
export type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; value: T; updating: boolean }
  | { status: "failed"; error: string; last: T | null; retry: () => void };

// --- The words (their one home in the console) -----------------------------------------------

/** The fit rule's reasons in the console's words (docs/module-planner.md "The photo fit rule").
 * No orientation words: fit is a photo's ratio, never a landscape or portrait label. */
export const REASON_WORDS: Readonly<Record<FitRefusal, string>> = {
  beyond_crop_limit: "doesn't fit this Frame's shape",
  below_frame_pixels: "too small for this Frame",
  face_cut: "a face would be cut",
  interest_cut: "the interesting part would be cut",
  no_video_here: "this Frame shows no videos",
};

export function reasonWords(reason: FitRefusal): string {
  return REASON_WORDS[reason];
}

/** Common ratios, wide to tall, as the console names them. */
const COMMON: readonly [string, number][] = [
  ["4:1", 4], ["3:1", 3], ["21:9", 21 / 9], ["2:1", 2], ["16:9", 16 / 9], ["5:3", 5 / 3], ["3:2", 1.5],
  ["4:3", 4 / 3], ["5:4", 1.25], ["1:1", 1], ["4:5", 0.8], ["3:4", 0.75], ["2:3", 2 / 3], ["3:5", 0.6],
  ["9:16", 9 / 16], ["1:2", 0.5], ["9:21", 9 / 21], ["1:3", 1 / 3], ["1:4", 0.25],
];

/** A ratio (width / height) in words: the nearest common ratio within 1.5 % ("3:2"), else its
 * value against 1 ("1.62:1", "1:1.62"). Never orientation words. */
export function ratioWords(ratio: number): string {
  const near = COMMON.find(([, value]) => Math.abs(Math.log(ratio / value)) < 0.015);
  if (near) return near[0];
  return ratio >= 1 ? `${ratio.toFixed(2)}:1` : `1:${(1 / ratio).toFixed(2)}`;
}

/** The Planner's words for a Frame with nothing that fits (docs/module-planner.md, the wall
 * plan's step 5). */
export const NOTHING_NEW_WORDS =
  "keeps its last photo while it still fits and is free; otherwise plays a photo not yet checked, centred; otherwise nothing new";

/** A Frame of the answer with nothing that fits, in the Planner's words: what it does. */
export function emptyFrameWords(frame: FrameFit, name = frame.frame_id): string {
  return `None of these ${countWords(frame.photos, "photo")} fit ${name}. ${name} ${NOTHING_NEW_WORDS}.`;
}

/** The sampled check of faces and the interesting part ("checked 64 of 1,520"), or null when
 * none was checked. Never extrapolated to the rest. */
export function checkedWords(frame: FrameFit): string | null {
  if (frame.checked === 0) return null;
  const kept = frame.fit_checked === frame.checked ? "every one keeps them whole"
    : `${countText(frame.fit_checked)} keep them whole`;
  return `Faces and the interesting part: checked ${countText(frame.checked)} of ${countText(frame.fit_by_shape)}; ${kept}.`;
}

/** Whether a Frame has photos that fit it in this answer (by shape and size, and, where some
 * were checked, by faces and subject too). */
export function hasFit(frame: FrameFit): boolean {
  return frame.fit_by_shape > 0 && (frame.checked === 0 || frame.fit_checked > 0);
}

/** A crop limit (a fraction) as whole percent: "35 %". */
export function percentWords(limit: number): string {
  return `${Math.round(limit * 100)} %`;
}

/** The answer in one line: "Fits 5 of 6 Frames · Hall has no photo that fits". */
export function lineWords(analysis: FitAnalysis, name: (frameId: string) => string = (id) => id): string {
  const frames = analysis.frames;
  if (frames.length === 0) return "There is no Frame to show them on yet";
  const empty = frames.filter((frame) => frame.photos > 0 && !hasFit(frame)).map((frame) => name(frame.frame_id));
  if (frames.length === 1) {
    const [frame] = frames;
    return empty.length > 0 ? `${empty[0]} has no photo that fits`
      : `${countText(frame.fit_by_shape)} of ${countText(frame.photos)} fit ${name(frame.frame_id)}`;
  }
  const fits = frames.length - empty.length;
  const lead = `Fits ${fits} of ${frames.length} Frames`;
  if (empty.length === 0) return lead;
  if (empty.length === 1) return `${lead} · ${empty[0]} has no photo that fits`;
  if (empty.length === 2) return `${lead} · ${empty[0]} and ${empty[1]} have no photo that fits`;
  return `${lead} · ${empty.length} Frames have no photo that fits`;
}

// --- The board's words and pictures ----------------------------------------------------------

/** The Shape map's marks: symmetric about 1:1. */
const TICKS = ["1:4", "9:16", "3:4", "1:1", "4:3", "16:9", "4:1"].map((label) => ({
  label, ratio: COMMON.find(([name]) => name === label)![1],
}));

function cropOf(sample: FitSample, aspect: number, verdict: string, refused = false): CropView {
  return {
    key: sample.asset_id,
    src: sample.thumbnail,
    alt: `${sample.kind === "video" ? "Video" : "Photo"}, ${ratioWords(sample.width / sample.height)}`,
    width: sample.width,
    height: sample.height,
    aspect,
    offset: sample.crop.offset,
    verdict,
    refused,
    video: sample.kind === "video",
  };
}

function size([width, height]: [number, number]): string {
  return `${countText(width)} × ${countText(height)}`;
}

/** The pixels a Frame is judged at, in words: its Frame output, never its Display's mode
 * (owner, 2026-10-11: "compatibility is measuring against the Frame output and not the
 * Display"). */
export function outputWords(frame: FrameFit): string {
  if (frame.output_px === null) {
    return "Judged by shape only: its Pi has not reported its display's size yet.";
  }
  const on = frame.display_px === null ? "" : ` · on a ${size(frame.display_px)} display`;
  return `Frame output ${size(frame.output_px)} px${on}`;
}

export interface BoardOptions {
  /** A Frame's name (its id by default). */
  name?: (frameId: string) => string;
  /** Where a Frame's name leads, if anywhere. */
  href?: (frameId: string) => string | undefined;
  /** What the board is judged at, in the host's words (from the answer, which says the crop
   * limit it used), or null for none. Default: "Judged at a crop limit of 35 %." */
  judged?: string | null | ((analysis: FitAnalysis) => string | null);
  /** One action for a Frame that has nothing that fits (the Scene editor's raise). */
  rescue?: (frame: FrameFit) => ProblemAction | undefined;
}

function frameView(frame: FrameFit, name: string, href: string | undefined): FrameFitView {
  const aspect = frame.output_px === null ? frame.aspect : frame.output_px[0] / frame.output_px[1];
  const shaped = frame.refused_by_shape.filter((count) => count.photos > 0);
  const segments: FitSegment[] = [
    { key: "fit", label: "fit", count: frame.fit_by_shape, countText: countText(frame.fit_by_shape) },
    ...shaped.map((count) => ({ key: count.reason, label: reasonWords(count.reason), count: count.photos,
      countText: countText(count.photos) })),
  ];
  const content = frame.refused_by_content.filter((count) => count.photos > 0);
  const reasons: FitReason[] = [
    ...shaped.map((count) => ({ key: count.reason, label: reasonWords(count.reason), countText: countText(count.photos),
      samples: count.samples.map((sample) => cropOf(sample, frame.aspect, reasonWords(count.reason), true)) })),
    ...content.map((count) => ({ key: count.reason, label: `${reasonWords(count.reason)} (of those checked)`,
      countText: countText(count.photos),
      samples: count.samples.map((sample) => cropOf(sample, frame.aspect, reasonWords(count.reason), true)) })),
  ];
  const left = frame.photos - frame.fit_by_shape;
  const summary = left > 0 ? `Why ${countText(left)} don't fit`
    : content.length > 0 ? "Why some of those checked don't fit" : null;
  return {
    key: frame.frame_id,
    name,
    aspect,
    ratio: ratioWords(frame.aspect),
    lead: frame.lead === null ? null : cropOf(frame.lead, frame.aspect, "fits"),
    // A shape answer draws no photo (its samples are the full answer's), so its empty tile
    // says nothing fits only when nothing does.
    emptyText: hasFit(frame) ? "Not checked yet" : "Nothing fits",
    count: `${countText(frame.fit_by_shape)} of ${countText(frame.photos)} fit`,
    segments,
    samples: frame.fitting.map((sample) => cropOf(sample, frame.aspect, "fits")),
    checked: checkedWords(frame),
    detail: outputWords(frame),
    why: summary === null ? null : { summary, reasons },
    draft: frame.draft ? "As you are setting it" : null,
    href,
  };
}

function binWords(bin: RatioBin, bands: { name: string; low: number; high: number }[]): ShapeBin {
  const middle = Math.sqrt(bin.low * bin.high);
  const holds = bands.filter((band) => middle >= band.low && middle <= band.high).map((band) => band.name);
  const parts = [bin.photos > 0 ? countWords(bin.photos, "photo") : null, bin.videos > 0 ? countWords(bin.videos, "video") : null]
    .filter((part): part is string => part !== null);
  return {
    low: bin.low, high: bin.high, total: bin.photos + bin.videos,
    label: ratioWords(middle),
    count: parts.join(" · ") || "none",
    frames: holds.length > 0 ? holds.join(", ") : "none",
  };
}

/** The most common shape of the selection's photos, in words, or null with none. */
function mostWords(analysis: FitAnalysis): string | null {
  const top = analysis.bins.reduce<RatioBin | null>((best, bin) =>
    (best === null || bin.photos + bin.videos > best.photos + best.videos ? bin : best), null);
  return top === null || top.photos + top.videos === 0 ? null : ratioWords(Math.sqrt(top.low * top.high));
}

/** Central's answer as the Fit board draws it (patterns/fit-board.tsx). */
export function boardView(analysis: FitAnalysis, options: BoardOptions = {}): FitBoardView {
  const name = options.name ?? ((id: string) => id);
  const most = mostWords(analysis);
  const warnings: FitWarning[] = analysis.frames
    .filter((frame) => frame.photos > 0 && !hasFit(frame))
    .map((frame) => {
      const shape = most === null ? "" : ` Most are ${most}; ${name(frame.frame_id)} is ${ratioWords(frame.aspect)}.`;
      return {
        key: frame.frame_id,
        subject: name(frame.frame_id),
        what: `${emptyFrameWords(frame, name(frame.frame_id))}${shape}`,
        action: options.rescue?.(frame),
      };
    });
  const bands = analysis.frames.map((frame) => ({ key: frame.frame_id, name: name(frame.frame_id),
    low: frame.band[0], high: frame.band[1] }));
  const total = analysis.photos + analysis.videos;
  const empty = total === 0
    ? (analysis.reading ? "Reading Immich: no photos found yet." : "No photos in this selection.")
    : analysis.frames.length === 0 ? "There is no Frame to show these photos on yet." : null;
  const counted = `${countWords(analysis.photos, "photo")} · ${countWords(analysis.videos, "video")}`;
  return {
    summary: analysis.reading ? `${counted} so far` : counted,
    line: lineWords(analysis, name),
    judged: options.judged === undefined ? `Judged at a crop limit of ${percentWords(analysis.crop_limit)}.`
      : typeof options.judged === "function" ? options.judged(analysis) : options.judged,
    reading: analysis.reading ? "Still reading Immich: these are the photos found so far." : null,
    warnings,
    frames: analysis.frames.map((frame) => frameView(frame, name(frame.frame_id), options.href?.(frame.frame_id))),
    shape: {
      bins: analysis.bins.map((bin) => binWords(bin, bands)),
      bands,
      range: [analysis.bins[0]?.low ?? 0.25, analysis.bins[analysis.bins.length - 1]?.high ?? 4],
      ticks: TICKS,
    },
    empty,
  };
}

/** A failed read in words. */
function failureWords(error: string): string {
  switch (error) {
    case "source_unknown":
      return "One of these Sources was deleted meanwhile.";
    case "draft_unknown":
      return "This unsaved Source expired. Discard it and start again.";
    case "frame_unknown":
      return "One of these Frames was deleted meanwhile.";
    case "unreachable":
      return "Central did not answer.";
    default:
      return `Central did not answer this (${error}).`;
  }
}

/** A read's state as the Fit board draws it: a failure with an answer before it keeps that
 * answer, greyed, as Can't tell since it was read. */
export function boardState(state: LoadState<FitAnalysis>, options: BoardOptions = {}): FitBoardState {
  switch (state.status) {
    case "loading":
      return { status: "loading" };
    case "ready":
      return { status: "ready", view: boardView(state.value, options), updating: state.updating };
    case "failed":
      return state.last === null
        ? { status: "error", what: failureWords(state.error), onRetry: state.retry }
        : {
          status: "cant-tell",
          view: boardView(state.last, options),
          what: `${failureWords(state.error)} These are the photos as read at ${sinceTime(state.last.read_at)}.`,
          onRetry: state.retry,
        };
  }
}

// --- The read ------------------------------------------------------------------------------

/** How long a change settles before the full answer is asked (a filter typed, a value set). */
export const SETTLE_MS = 400;
/** While a value is dragged (`live`), how soon after each move the shape answer is asked. */
export const LIVE_MS = 150;
/** How often a shown board asks again: a pool still being read fills in. */
export const CADENCE_MS = 30_000;

/**
 * How `request` fits the Frames, read from Central's one analysis: the full answer (or the
 * `tier` asked for: a line on a list asks "shape") {@link SETTLE_MS} after the last change; with `live` (a value being dragged) the shape tier
 * {@link LIVE_MS} after each change, and the full answer once `live` ends. Asked again every
 * {@link CADENCE_MS} while the page is visible. The last answer stays shown (`updating`) while a
 * newer one is read, and a failed read keeps it. A null request reads nothing. A new
 * `refreshKey` asks again for the same question (a draft's pool that changed under it).
 */
export function useFitAnalysis(request: FitAnalysisRequest | null,
  options: { live?: boolean; cadenceMs?: number; tier?: "shape" | "full"; refreshKey?: string } = {}): LoadState<FitAnalysis> {
  const live = options.live ?? false;
  const refreshKey = options.refreshKey ?? "";
  const settled = options.tier ?? "full";
  const cadence = options.cadenceMs ?? CADENCE_MS;
  const key = request === null ? null : JSON.stringify(request);
  const [answer, setAnswer] = useState<{ key: string; value: FitAnalysis } | null>(null);
  const [failure, setFailure] = useState<{ key: string; error: string } | null>(null);
  const [asking, setAsking] = useState(false);
  const [again, setAgain] = useState(0);
  const sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);

  const ask = useCallback(async (question: string, tier: "shape" | "full") => {
    const number = ++sequence.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setAsking(true);
    const body = { ...(JSON.parse(question) as FitAnalysisRequest), tier };
    const result = await analyseFit(body, abort.signal) as { ok: true; analysis: FitAnalysis } | { ok: false; error: string };
    if (number !== sequence.current) return; // a newer question replaced it
    setAsking(false);
    if (result.ok) {
      setAnswer({ key: question, value: result.analysis });
      setFailure(null);
    } else if (result.error !== "aborted") {
      setFailure({ key: question, error: result.error });
    }
  }, []);

  useEffect(() => {
    if (key === null) return undefined;
    const timer = setTimeout(() => void ask(key, live ? "shape" : settled), live ? LIVE_MS : SETTLE_MS);
    return () => clearTimeout(timer);
  }, [key, live, settled, again, refreshKey, ask]);

  useEffect(() => {
    if (key === null || live) return undefined;
    const timer = setInterval(() => {
      if (typeof document === "undefined" || document.visibilityState === "visible") void ask(key, settled);
    }, cadence);
    return () => clearInterval(timer);
  }, [key, live, settled, cadence, ask]);

  useEffect(() => () => controller.current?.abort(), []);

  const retry = useCallback(() => setAgain((count) => count + 1), []);
  return useMemo((): LoadState<FitAnalysis> => {
    if (failure !== null && failure.key === key && !asking) {
      return { status: "failed", error: failure.error, last: answer?.value ?? null, retry };
    }
    if (answer === null) return { status: "loading" };
    return { status: "ready", value: answer.value, updating: asking || answer.key !== key };
  }, [answer, failure, asking, key, retry]);
}

// --- Settings › Photo fit: the saved values, their defaults and ranges ---------------------------

/** GET /v1/operator/settings/photo-fit (`FitSettingsView`): the ranges are Central's, so the
 * console never copies a bound. */
export interface FitSettingsView {
  settings: FitSettings;
  defaults: FitSettings;
  ranges: Record<keyof FitSettings, [number, number]>;
}

export type SettingsRead =
  | { status: "loading" }
  | { status: "read"; view: FitSettingsView }
  | { status: "failed"; error: string };

/**
 * The house's photo fit settings, read once when shown and again after `save`. `save` sends
 * every value (a PUT of the whole `FitSettings`) and answers the refusal's code, or null.
 */
export function useFitSettings(): { read: SettingsRead; reload: () => void;
  save: (settings: FitSettings) => Promise<string | null> } {
  const [read, setRead] = useState<SettingsRead>({ status: "loading" });
  const [again, setAgain] = useState(0);
  useEffect(() => {
    let live = true;
    void readFitSettings().then((answer) => {
      if (!live) return;
      const result = answer as { ok: true; view: FitSettingsView } | { ok: false; error: string };
      setRead(result.ok ? { status: "read", view: result.view } : { status: "failed", error: result.error });
    });
    return () => {
      live = false;
    };
  }, [again]);
  const reload = useCallback(() => setAgain((count) => count + 1), []);
  const save = useCallback(async (settings: FitSettings) => {
    const result = await saveFitSettings(settings) as { ok: true; view: FitSettingsView } | { ok: false; error: string };
    if (!result.ok) return result.error;
    setRead({ status: "read", view: result.view });
    return null;
  }, []);
  return { read, reload, save };
}

/** The house crop limit, once read (null until then, or when the read failed). */
export function useHouseCropLimit(): number | null {
  const { read } = useFitSettings();
  return read.status === "read" ? read.view.settings.crop_limit : null;
}

// --- The stored Scenes, as the moments ask about them ------------------------------------------

/** A stored Scene as a fit question: its Sources, its Frames and its crop limit (null: it
 * follows the house's). `sourceRefs` are every Source it names, or, from {@link scenesOnFrame},
 * those it shows on that Frame. */
export interface SceneUse {
  sceneId: string;
  sourceRefs: string[];
  frames: string[];
  cropLimit: number | null;
}

interface StoredContribution { target?: string; source_refs?: string[] }
interface StoredScene {
  scene_id?: string;
  selection?: { crop_limit?: number | null };
  contributions?: StoredContribution[];
  outro_contributions?: StoredContribution[];
  children?: { scene?: StoredScene }[];
}

/**
 * Every stored Scene that shows photos from a Source (the snapshot's runtime `definitions`),
 * with the Frames it shows them on: its body's and outro's contributions, and an inline child
 * Scene's as its own (judged at the child's crop limit). Sorted by id.
 */
export function storedScenes(snapshot: object | null): SceneUse[] {
  const definitions = (snapshot as { runtime?: { definitions?: Record<string, StoredScene> } } | null)
    ?.runtime?.definitions ?? {};
  const found = new Map<string, SceneUse & { pairs: [string, string][] }>();
  const visit = (scene: StoredScene | undefined, sceneId: string) => {
    if (scene === undefined) return;
    const cropLimit = typeof scene.selection?.crop_limit === "number" ? scene.selection.crop_limit : null;
    const pairs: [string, string][] = [];
    for (const entry of [...(scene.contributions ?? []), ...(scene.outro_contributions ?? [])]) {
      const frame = frameOf(entry.target ?? "");
      if (frame === null) continue;
      for (const sourceRef of entry.source_refs ?? []) pairs.push([frame, sourceRef]);
    }
    if (pairs.length > 0) {
      found.set(sceneId, {
        sceneId, cropLimit, pairs,
        sourceRefs: [...new Set(pairs.map(([, ref]) => ref))].sort(),
        frames: [...new Set(pairs.map(([frame]) => frame))].sort(),
      });
    }
    for (const child of scene.children ?? []) visit(child.scene, child.scene?.scene_id ?? sceneId);
  };
  for (const [sceneId, scene] of Object.entries(definitions)) visit(scene, scene.scene_id ?? sceneId);
  return [...found.values()].sort((a, b) => a.sceneId.localeCompare(b.sceneId))
    .map(({ sceneId, sourceRefs, frames, cropLimit }) => ({ sceneId, sourceRefs, frames, cropLimit }));
}

/** The stored Scenes that show photos on `frameId`, each with only the Sources it shows there. */
export function scenesOnFrame(snapshot: object | null, frameId: string): SceneUse[] {
  const definitions = (snapshot as { runtime?: { definitions?: Record<string, StoredScene> } } | null)
    ?.runtime?.definitions ?? {};
  const found = new Map<string, SceneUse>();
  const visit = (scene: StoredScene | undefined, sceneId: string) => {
    if (scene === undefined) return;
    const cropLimit = typeof scene.selection?.crop_limit === "number" ? scene.selection.crop_limit : null;
    const refs = new Set<string>();
    for (const entry of [...(scene.contributions ?? []), ...(scene.outro_contributions ?? [])]) {
      if (frameOf(entry.target ?? "") !== frameId) continue;
      for (const sourceRef of entry.source_refs ?? []) refs.add(sourceRef);
    }
    if (refs.size > 0) found.set(sceneId, { sceneId, sourceRefs: [...refs].sort(), frames: [frameId], cropLimit });
    for (const child of scene.children ?? []) visit(child.scene, child.scene?.scene_id ?? sceneId);
  };
  for (const [sceneId, scene] of Object.entries(definitions)) visit(scene, scene.scene_id ?? sceneId);
  return [...found.values()].sort((a, b) => a.sceneId.localeCompare(b.sceneId));
}

/** The stored Scenes that use the Source named like `sourceRef` (any revision of it). */
export function scenesUsingSource(snapshot: object | null, sourceRef: string): SceneUse[] {
  const name = sourceName(sourceRef);
  return storedScenes(snapshot).filter((scene) => scene.sourceRefs.some((ref) => sourceName(ref) === name));
}
