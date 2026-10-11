import { useState } from "react";
import type * as React from "react";

import { Button } from "../ui/button";
import { cn } from "../ui/cn";
import { Disclosure } from "../ui/disclosure";
import { Sheet } from "../ui/sheet";
import type { CropView } from "./crop-thumb";
import { EmptyState } from "./entity-page";
import { FrameFitRow, type FrameFitView } from "./frame-fit-row";
import { type ProblemAction, ProblemCard } from "./problem-card";
import { type ShapeMapProps, ShapeMap } from "./shape-map";
import { WallStrip } from "./wall-strip";

/** A Frame that will go without: shown on top of the board, worst first. */
export interface FitWarning {
  key: string;
  /** Its verdict in two words ("Nothing fits", "Few photos"). */
  label: string;
  /** The Frame's name. */
  subject: string;
  /** What happens, in plain words (the Planner's words for a Frame with nothing new). */
  what: string;
  action?: ProblemAction;
}

/** What a Fit board shows, in words and pictures (the domain builds it from Central's answer). */
export interface FitBoardView {
  /** The selection's counts ("1,689 photos · 42 videos"). */
  summary: string;
  /** The board in one line ("Fits 5 of 6 Frames · Hall has no photo that fits"). */
  line: string;
  /** What it is judged at ("Judged at the house crop limit, 35 %"), or null. */
  judged: string | null;
  /** While the selection is still being read ("Still reading Immich: these are the photos
   * found so far."), or null. */
  reading: string | null;
  warnings: readonly FitWarning[];
  frames: readonly FrameFitView[];
  shape: Omit<ShapeMapProps, "focused">;
  /** Nothing to judge (no photo in the selection, or no Frame), in words; null otherwise. */
  empty: string | null;
}

/** Where a board's answer stands. */
export type FitBoardState =
  | { status: "loading" }
  | { status: "ready"; view: FitBoardView; updating: boolean }
  | { status: "cant-tell"; view: FitBoardView; what: string; onRetry: () => void }
  | { status: "error"; what: string; onRetry: () => void };

/** What leads a board, as its host previews it (owner, 2026-10-11: "preview is an idea,
 * rather than a specific widget. Because what is being previewed is context dependent"):
 * `frames`, each Frame's row (a Frame's own page, the Settings); `shapes`, the selection's
 * shapes first, then the Frames they can feed (a Source being edited); `wall`, the Frames side
 * by side, each filled with a different photo, then their rows (a Scene filling its Frames
 * together). */
export type FitBoardLead = "frames" | "shapes" | "wall";

export interface FitBoardProps {
  state: FitBoardState;
  /** `full`: every Frame with its photos; `compact`: the Frames' counts; `strip`: one line and
   * the Frames side by side, small, opening the full board in a sheet; `line`: one line that
   * opens the full board in a sheet. */
  size: "full" | "compact" | "strip" | "line";
  /** What leads a `full` or `compact` board (and the sheet a `strip` or `line` opens). */
  lead?: FitBoardLead;
  /** False when the host already names the Frame (a Frame's own page). */
  named?: boolean;
  /** The board's accessible name and the sheet's title ("How these photos fit the Frames"). */
  title?: string;
  /** A heading shown over a `full` or `compact` board ("Which Frames can show them"). */
  heading?: string;
  /** Draws other photos as samples (no side effect). */
  onShuffle?: () => void;
  /** A Frame was picked (its tile pressed). */
  onFrame?: (frameKey: string) => void;
  /** One action for a Frame, from the host. */
  action?: (frameKey: string) => React.ReactNode;
  /** Opens a photo (its sheet). */
  onOpen?: (sample: CropView, frameKey: string) => void;
  /** On a phone a `full` board is its line, opening the board in a sheet; false keeps it full
   * (a host already in a sheet). */
  collapse?: boolean;
}

function Skeleton() {
  return (
    <div role="group" aria-busy="true" aria-label="Reading how these photos fit" className="flex min-w-0 flex-col gap-3">
      {[0, 1, 2].map((row) => (
        <div key={row} className="flex items-center gap-3">
          <div className="h-14 w-24 rounded-input bg-surface-sunken" />
          <div className="flex flex-1 flex-col gap-2">
            <div className="h-3 w-1/3 rounded-pill bg-surface-sunken" />
            <div className="h-2.5 w-full rounded-pill bg-surface-sunken" />
          </div>
        </div>
      ))}
    </div>
  );
}

function Board({ view, size, lead, named, faded, onShuffle, onFrame, action, onOpen }: {
  view: FitBoardView; size: "full" | "compact"; lead: FitBoardLead; named: boolean; faded: boolean;
} & Pick<FitBoardProps, "onShuffle" | "onFrame" | "action" | "onOpen">) {
  const [focused, setFocused] = useState<string | null>(null);
  if (view.empty !== null) return <EmptyState>{view.empty}</EmptyState>;
  const shapes = <ShapeMap {...view.shape} focused={focused} />;
  const rows = (
    <div className="flex min-w-0 flex-col">
      {view.frames.map((frame) => (
        <FrameFitRow key={frame.key} frame={frame} size={size} faded={faded} named={named}
          onOpen={onOpen ? (sample) => onOpen(sample, frame.key) : undefined}
          onFocus={() => {
            setFocused(frame.key);
            onFrame?.(frame.key);
          }}
          action={action?.(frame.key)} />
      ))}
    </div>
  );
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p className="m-0 text-sm text-text">{view.summary}</p>
        {onShuffle && (size === "full" || lead === "wall") ? (
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={onShuffle}>Shuffle photos</Button>
        ) : null}
      </div>
      {view.judged !== null && <p className="m-0 text-xs text-muted">{view.judged}</p>}
      {view.reading !== null && <p className="m-0 text-xs text-muted">{view.reading}</p>}
      {lead === "shapes" && (
        <section aria-label="The shapes of these photos" className="flex min-w-0 flex-col gap-2">
          <h5 className="m-0 text-sm font-medium text-text">Their shapes, and the shapes each Frame takes</h5>
          {shapes}
        </section>
      )}
      {lead === "wall" && (
        <WallStrip frames={view.frames} faded={faded} onFrame={(key) => {
          setFocused(key);
          onFrame?.(key);
        }} />
      )}
      {view.warnings.map((warning) => (
        <ProblemCard key={warning.key} scope="setup" subject={warning.subject} variant="inline"
          verdict={{ severity: "todo", label: warning.label, receipt: null }}
          what={warning.what} doing={null} action={warning.action} />
      ))}
      {lead === "shapes" ? (
        <section aria-label="Which Frames can show them" className="flex min-w-0 flex-col">
          <h5 className="m-0 text-sm font-medium text-text">Which Frames can show them</h5>
          {rows}
        </section>
      ) : rows}
      {lead !== "shapes" && size === "full" && (
        <Disclosure summary="Why: the shapes of these photos">{shapes}</Disclosure>
      )}
    </div>
  );
}

/** A thin line over a board whose newer answer is on its way (never a spinner over a blank
 * board); still while reduced motion is asked for. */
function Updating() {
  return (
    <div aria-hidden="true" className="h-0.5 w-full overflow-hidden rounded-pill bg-surface-sunken">
      <div className="h-full w-1/3 animate-pulse rounded-pill bg-muted motion-reduce:animate-none" />
    </div>
  );
}

type BodyProps = Omit<FitBoardProps, "size" | "collapse" | "title" | "lead" | "named"> & {
  size: "full" | "compact"; lead: FitBoardLead; named: boolean;
};

function Body(props: BodyProps) {
  const { state } = props;
  switch (state.status) {
    case "loading":
      return <Skeleton />;
    case "error":
      return (
        <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
          what={state.what} doing={null} action={{ label: "Try again", onAction: state.onRetry }} />
      );
    case "cant-tell":
      return (
        <div className="flex min-w-0 flex-col gap-3">
          <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
            what={state.what} doing={null} action={{ label: "Try again", onAction: state.onRetry }} />
          <Board {...props} view={state.view} faded />
        </div>
      );
    case "ready":
      return (
        <div className="flex min-w-0 flex-col gap-2" aria-busy={state.updating}>
          {state.updating ? <Updating /> : <div className="h-0.5" />}
          <Board {...props} view={state.view} faded={state.updating} />
        </div>
      );
  }
}

function lineOf(state: FitBoardState): string {
  switch (state.status) {
    case "loading":
      return "Reading how these photos fit…";
    case "error":
      return "Can't tell how these photos fit";
    default:
      return state.view.empty ?? state.view.line;
  }
}

/** The one line (with, as a `strip`, the Frames side by side under it) and the full board it
 * opens in a sheet. */
function Line({ title, state, strip = false, lead, named, ...rest }: Omit<FitBoardProps, "size" | "collapse" | "lead" | "named"> & {
  title: string; strip?: boolean; lead: FitBoardLead; named: boolean;
}) {
  const [open, setOpen] = useState(false);
  const view = state.status === "ready" || state.status === "cant-tell" ? state.view : null;
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "flex min-h-11 w-full min-w-0 cursor-pointer items-center justify-between gap-3 rounded-input border border-line",
          "bg-surface-raised px-3 py-2 text-left font-sans text-sm text-text",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        )}
      >
        <span className="min-w-0 wrap-anywhere">{lineOf(state)}</span>
        <span className="shrink-0 text-accent" aria-hidden="true">Show ›</span>
      </button>
      {strip && view !== null && view.empty === null && (
        <WallStrip frames={view.frames} size="small" faded={state.status !== "ready" || state.updating}
          onFrame={() => setOpen(true)} />
      )}
      <Sheet open={open} onOpenChange={setOpen} title={title}>
        {open ? <Body state={state} size="full" lead={lead} named={named} {...rest} /> : null}
      </Sheet>
    </div>
  );
}

/**
 * FitBoard (design language §4, follow-ups C1 and review): how a selection of photos fits the
 * Frames, composed for its moment (`lead`, `size`): a Source's shapes then the Frames they feed,
 * a Scene's Frames side by side then their rows, a Frame's own count, or one line with a strip.
 * Each Frame is drawn at its output's shape and filled with what it would show, with how many
 * photos fit, a bar of why the rest do not and, at `full`, photos cropped as it crops them;
 * Frames with nothing that fits, or too few to change, are warned of on top. The board judges nothing: it draws the words and pictures its host's domain
 * built from Central's one analysis. While a newer answer is read the board stays, dimmed, under
 * a thin line; a failed read keeps the last answer, greyed, as Can't tell.
 */
export function FitBoard({ state, size, lead = "frames", named = true, title = "How these photos fit the Frames", heading,
  collapse = true, ...rest }: FitBoardProps) {
  if (size === "line" || size === "strip") {
    return (
      <section aria-label={title} className="min-w-0">
        <Line title={title} state={state} strip={size === "strip"} lead={lead} named={named} {...rest} />
      </section>
    );
  }
  const body = (
    <>
      {heading ? <h4 className="m-0 mb-2 text-sm font-medium text-text">{heading}</h4> : null}
      <Body state={state} size={size} lead={lead} named={named} {...rest} />
    </>
  );
  if (size === "compact" || !collapse) return <section aria-label={title} className="min-w-0">{body}</section>;
  return (
    <>
      <div className="md:hidden"><Line title={title} state={state} lead={lead} named={named} {...rest} /></div>
      <section aria-label={title} className="hidden min-w-0 md:block">{body}</section>
    </>
  );
}
