import { useMemo, useState } from "react";
import type * as React from "react";

import type { CropView } from "../patterns/crop-thumb";
import { FitBoard, type FitBoardLead } from "../patterns/fit-board";
import type { FrameFitView } from "../patterns/frame-fit-row";
import type { ProblemAction } from "../patterns/problem-card";
import { FrameFitTile } from "../patterns/wall-strip";
import {
  type BoardOptions,
  boardState,
  type FitAnalysis,
  type FitAnalysisRequest,
  type FrameFit,
  type LoadState,
  lineWords,
  ratioWords,
  useFitAnalysis,
} from "./photo-fit";
import { PhotoSheet } from "./photo-sheet";

/** A fit question without its tier and seed: the board asks each tier, and Shuffle owns the seed. */
export type FitQuestion = Omit<FitAnalysisRequest, "tier" | "seed">;

export interface FitRead {
  state: LoadState<FitAnalysis>;
  /** Draws other photos as samples. */
  shuffle: () => void;
}

/**
 * One host's read of its fit question (photo-fit.ts `useFitAnalysis`), with the seed Shuffle
 * moves. `live`: a value is being dragged (the shape tier answers each move).
 */
export function useFitRead(question: FitQuestion | null,
  options: { live?: boolean; tier?: "shape" | "full"; refreshKey?: string } = {}): FitRead {
  const [seed, setSeed] = useState(0);
  const request = useMemo(() => (question === null ? null : { ...question, seed }), [question, seed]);
  const state = useFitAnalysis(request, options);
  return { state, shuffle: () => setSeed((current) => (current + 1) % 2_147_483_647) };
}

/** A read in one line, for a host's own line (a phone's count line, a card): null while it
 * has no answer. */
export function fitLine(state: LoadState<FitAnalysis>, name?: (frameId: string) => string): string | null {
  const value = state.status === "ready" ? state.value : state.status === "failed" ? state.last : null;
  return value === null ? null : lineWords(value, name);
}

/** Whether a board drawn from this read shows its own "so far" count (Immich is still being
 * read, and there are photos and Frames to count), so a host's older count of the same photos
 * hides while it runs and the page shows one count. */
export function fitReading(state: LoadState<FitAnalysis>): boolean {
  const value = state.status === "ready" ? state.value : state.status === "failed" ? state.last : null;
  return value !== null && value.reading && value.frames.length > 0 && value.photos + value.videos > 0;
}

interface InventoryFrame { id: string; profile?: { width_px?: number; height_px?: number } }

/** A Frame not judged yet (no photos chosen): drawn at its profile's shape (its Frame output's
 * shape: the picture keeps the profile's shape inside its Position), empty. */
function unjudged(snapshot: object | null, frameId: string, words: string): FrameFitView | null {
  const frames = (snapshot as { inventory?: { frames?: InventoryFrame[] } } | null)?.inventory?.frames ?? [];
  const profile = frames.find((frame) => frame.id === frameId)?.profile;
  const width = profile?.width_px ?? 0;
  const height = profile?.height_px ?? 0;
  if (width <= 0 || height <= 0) return null;
  return {
    key: frameId, name: frameId, aspect: width / height, ratio: ratioWords(width / height), lead: null, emptyText: "",
    count: words, segments: [], samples: [], checked: null, unknownOutput: null, detail: null, alert: null, why: null,
    draft: null,
  };
}

/**
 * The Frame picker's tiles (the Scene's Frames step, design review round 2): each Frame drawn at
 * its Frame output's shape with the photo it would show and how many of the chosen photos fit
 * it (its alert's chip if it would go without or have few), from the one read of every Frame;
 * before any photo is chosen (`read` null), at its profile's shape, empty. `sharing`: the Frames
 * chosen so far, whom the photos are shared between.
 */
export function frameChoiceTiles(read: FitRead | null, snapshot: object | null, sharing: number,
  name?: (frameId: string) => string): (frameId: string) => React.ReactNode {
  const state = read === null ? null : boardState(read.state, { name, sharing: Math.max(sharing, 1) });
  const view = state !== null && (state.status === "ready" || state.status === "cant-tell") ? state.view : null;
  const faded = state !== null && (state.status === "cant-tell" || (state.status === "ready" && state.updating));
  const judged = new Map((view?.frames ?? []).map((frame) => [frame.key, frame]));
  const pending = read === null ? "Choose its photos next" : view?.empty ?? "Reading what fits…";
  return (frameId) => {
    const frame = judged.get(frameId) ?? unjudged(snapshot, frameId, pending);
    return frame === null ? null : <FrameFitTile frame={frame} named={false} faded={faded} brief />;
  };
}

export interface PhotoFitBoardProps extends BoardOptions {
  read: FitRead;
  size: "full" | "compact" | "strip" | "line";
  /** What leads the board at this moment (patterns/fit-board.tsx `FitBoardLead`). */
  lead?: FitBoardLead;
  /** False when the host already names the Frame. */
  named?: boolean;
  title?: string;
  /** A heading shown over the board ("Which Frames can show them"). */
  heading?: string;
  /** One action under a Frame, from its host. */
  action?: (frameKey: string) => React.ReactNode;
  /** False keeps a `full` board full on a phone (a host already in a sheet). */
  collapse?: boolean;
}

/**
 * The Fit board of one moment (follow-ups C1): its read drawn by the FitBoard pattern, in the
 * words of photo-fit.ts, with Shuffle, and the photo sheet a sample opens (C2). Each host
 * builds its own question (what is being previewed is the host's, owner 2026-10-11); this is
 * the one place a read becomes a board.
 */
export function PhotoFitBoard({ read, size, lead, named, title, heading, action, collapse, ...options }: PhotoFitBoardProps) {
  const [open, setOpen] = useState<{ sample: CropView; frameKey: string } | null>(null);
  const state = boardState(read.state, options);
  const answer = read.state.status === "ready" ? read.state.value
    : read.state.status === "failed" ? read.state.last : null;
  return (
    <>
      <FitBoard state={state} size={size} lead={lead} named={named} title={title} heading={heading} action={action} collapse={collapse}
        onShuffle={read.shuffle} onOpen={(sample, frameKey) => setOpen({ sample, frameKey })} />
      <PhotoSheet
        open={open !== null}
        onClose={() => setOpen(null)}
        assetId={open?.sample.key ?? null}
        sample={open?.sample ?? null}
        frames={answer?.frames ?? []}
        focus={open?.frameKey ?? null}
        name={options.name}
      />
    </>
  );
}

export interface FitBoardForProps extends Omit<PhotoFitBoardProps, "read"> {
  question: FitQuestion | null;
  live?: boolean;
  tier?: "shape" | "full";
}

/** A Fit board that reads its own question. */
export function FitBoardFor({ question, live, tier, size, lead, named, title, heading, action, collapse, name, href,
  judged, rescue, lineOutput }: FitBoardForProps) {
  const read = useFitRead(question, { live, tier });
  return (
    <PhotoFitBoard read={read} size={size} lead={lead} named={named} title={title} heading={heading} action={action}
      collapse={collapse} name={name} href={href} judged={judged} rescue={rescue} lineOutput={lineOutput} />
  );
}

/** "Raise the crop limit to 45 %": a Frame with nothing that fits, offered the smallest crop
 * limit at which some photo would (Central's `nearest_crop_limit`), when `raise` can set it. */
export function raiseAction(frame: FrameFit, raise: ((limit: number) => void) | null): ProblemAction | undefined {
  if (raise === null || frame.nearest_crop_limit === null) return undefined;
  const limit = Math.ceil(frame.nearest_crop_limit * 100) / 100;
  return { label: `Raise the crop limit to ${Math.round(limit * 100)} %`, onAction: () => raise(limit) };
}
