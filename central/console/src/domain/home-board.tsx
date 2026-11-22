import type * as React from "react";

import type { Action, FramePlace, HomeView, Override, WallView } from "../generated/contract";
import { WideOnly } from "../patterns/app-shell";
import { EmptyState } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { HealthBadge } from "../patterns/health-badge";
import { OverrideBanner } from "../patterns/override-banner";
import { ProblemCard } from "../patterns/problem-card";
import { NowNextStrip, WallMap } from "../patterns/wall-map";
import { WallTile } from "../patterns/wall-tile";
import { POWER } from "../powerWords.js";
import { formatRoute } from "../routes.js";
import { Button } from "../ui/button";
import { Link } from "../ui/link";
import type { MenuItem } from "../ui/menu";
import { Skeleton } from "../ui/skeleton";
import { Inline, Stack } from "../ui/stack";
import {
  ACTION_ORDER, ACTION_WORDS, type FixView, frameChip, frameHref, framePlaces, houseCounts, houseVerdict, instantWords, nowNextWords,
  overrideView, type ProblemView, problemView, statusOf, tileLine,
} from "./frame-status";

/** What Home has of its read: none yet, none at all, or a document (with since when it is not current). */
export type HomeState =
  | { kind: "loading" }
  /** Central did not answer and there is no document to show. */
  | { kind: "error" }
  /** `staleSince`: Central's time of the last read that answered, when the newest one did not. */
  | { kind: "ready"; home: HomeView; staleSince: number | null };

export interface HomeBoardProps {
  state: HomeState;
  /** Central's time of the read: every "since" and "until" is said from it. */
  now: number | null;
  /** Each Pi in words by its device id ("Pi 3f2a9c"). */
  pis: Readonly<Record<string, string>>;
  /** The Pis with a request in progress, each with what its tiles say ("Restart requested"). */
  requested: Readonly<Record<string, string>>;
  /** A press is on its way to Central: the buttons wait. */
  busy: boolean;
  /** What the last press came to, in words. */
  said: string | null;
  /** A problem's fix the system performs (not a link). */
  onFix: (fix: NonNullable<FixView["fix"]>) => void;
  /** An action from a tile's menu. */
  onAction: (place: FramePlace, action: Action) => void;
  /** All off or All on. */
  onHold: (power: "on" | "off") => void;
  /** Resume schedule on a hold's Frames. */
  onResume: (override: Override) => void;
  /** A tile was tapped: its Frame's page takes focus. */
  onOpenFrame?: (frameId: string, event: React.MouseEvent<HTMLAnchorElement>) => void;
  /** The requests in progress, each a `StepProgress`. */
  progress?: React.ReactNode;
  /** What other reads found wrong (Pis, room lights, the photo library, the photo app's reports):
   * the same list of what is wrong, after Central's own. */
  more?: React.ReactNode;
  /** How a tile's menu opens (the catalogue fixes it; the app follows the screen). */
  menuAs?: "auto" | "popup" | "sheet";
}

const WALL_HREF = formatRoute({ section: "wall" });
const SHOW_NOW_HREF = formatRoute({ section: "now", flow: "show", step: "scene" });
const overview = (frameId: string) => formatRoute({ section: "wall", id: frameId, tab: "overview" });

/** One wall's tiles: each Frame's name, its chip, what it is doing, and what may be asked of it now. */
function wallFrames(wall: WallView, props: HomeBoardProps, stale: string | undefined) {
  const { now, pis, requested, onAction, onOpenFrame, menuAs } = props;
  return wall.frames.map((place) => {
    const { status } = place;
    const asked = place.device_id != null ? requested[place.device_id] : undefined;
    // An action is offered only when Central's `can` holds it, never while one is on its way, and
    // never from a document that is not current: what may be asked now is then not known.
    const actions: MenuItem[] = asked !== undefined || stale !== undefined ? []
      : ACTION_ORDER.filter((action) => (status.can ?? []).includes(action))
        .map((action) => ({ label: ACTION_WORDS[action], onAction: () => onAction(place, action) }));
    const items: MenuItem[] = [...actions,
      { label: "Why is this playing?", href: overview(place.frame_id) },
      { label: "Open Frame", href: frameHref(place.frame_id, status) }];
    return {
      key: place.frame_id,
      x: place.x_mm,
      y: place.y_mm,
      size: place.size_mm,
      tile: (
        <WallTile name={place.name} href={frameHref(place.frame_id, status)}
          onOpen={onOpenFrame === undefined ? undefined : (event) => onOpenFrame(place.frame_id, event)}
          chip={frameChip(status)}
          line={tileLine(place, { pi: place.device_id != null ? pis[place.device_id] ?? null : null, now })}
          stale={stale} requested={asked}
          // A Frame still in setup has no actions: its tile opens its page, where setup is.
          menu={status.setup !== null ? undefined : { label: `More actions for ${place.name}`, items, as: menuAs }} />
      ),
    };
  });
}

/** One of Central's problems as the error template, with its one fix. */
function Problem({ view, busy, stale, requested, onFix }: {
  view: ProblemView; busy: boolean; stale: boolean; requested: Readonly<Record<string, string>>; onFix: HomeBoardProps["onFix"];
}) {
  const { fix } = view;
  // A fix asked of a Pi is not offered again while that Pi has a request on its way; Carry on has
  // nothing to carry on from until pauses exist. From a document that is not current only a link
  // is offered: a fix the system performs is asked of what Central says now.
  const offered = fix === null || fix.fix?.kind === "carry-on" || (stale && fix.fix !== undefined) ? null
    : fix.fix !== undefined && "device_id" in fix.fix && requested[fix.fix.device_id] !== undefined ? null : fix;
  return (
    <ProblemCard scope="live" subject={view.subject} since={view.since} verdict={view.verdict} what={view.what}
      doing={view.doing}
      action={offered === null ? undefined
        : offered.href !== undefined ? { label: offered.label, href: offered.href }
          : { label: offered.label, onAction: () => { if (!busy) onFix(offered.fix); } }}
      details={(
        <Stack>
          {view.details.map(([label, value]) => <Note key={label}>{`${label}: ${value}`}</Note>)}
        </Stack>
      )} />
  );
}

/**
 * Home for one read (T1; decision 0036): what a story draws. Top to bottom: the house word and its
 * one sentence; what is wrong, worst first, each with its one fix and Details; the requests in
 * progress; what a person put over the schedule (a display power hold, with **Resume schedule**);
 * each wall, read-only, its Frames as tiles with now and next under it; the quick actions. It
 * judges nothing: every word and severity is domain/frame-status.ts's reading of Central's codes.
 */
export function HomeBoard(props: HomeBoardProps) {
  const { state, now, pis, requested, busy, said, onFix, onHold, onResume, progress, more } = props;
  if (state.kind === "loading") {
    return (
      <Stack>
        <Skeleton shape="line" />
        <WallMap name="Reading the house" loading frames={[]} />
      </Stack>
    );
  }
  if (state.kind === "error") {
    return (
      <Stack>
        <HealthBadge verdict={houseVerdict(null)} />
        <CentralDidNotAnswer since={null} />
        {more}
      </Stack>
    );
  }
  const { home, staleSince } = state;
  const places = framePlaces(home);
  const names = Object.fromEntries(places.map((place) => [place.frame_id, place.name]));
  const stale = staleSince === null ? undefined : `Can't tell since ${instantWords(staleSince, now)}`;
  const problems = (home.house.problems ?? []).map((problem) => {
    const place = problem.frame_id == null ? null : places.find((candidate) => candidate.frame_id === problem.frame_id) ?? null;
    return problemView(problem, place?.status ?? (problem.frame_id == null ? null : statusOf(home, problem.frame_id)),
      place?.name ?? problem.frame_id ?? null,
      { pi: place?.device_id != null ? pis[place.device_id] ?? null : null, now });
  });
  // What exists today: display power holds. A takeover and a pause have their banner kinds, and
  // nothing draws them here until their way back exists (slice 3).
  const holds = home.overrides.filter((override) => override.kind === "power-hold");
  return (
    <Stack>
      <Stack label="House">
        <HealthBadge verdict={{ ...houseVerdict(staleSince === null ? home.house : null), receipt: houseCounts(home) }} />
      </Stack>
      {(staleSince !== null || problems.length > 0 || more != null) && (
        <Stack label="Problems">
          {staleSince !== null && <CentralDidNotAnswer since={instantWords(staleSince, now)} />}
          {problems.map((view) => <Problem key={view.key} view={view} busy={busy} stale={staleSince !== null} requested={requested} onFix={onFix} />)}
          {more}
        </Stack>
      )}
      {progress}
      {holds.map((override) => {
        const view = overrideView(override, names, now);
        return (
          <OverrideBanner key={override.id} kind={view.kind} label={view.label} title={view.title}
            action={{ label: view.back, onAction: () => onResume(override), busy }}>
            <Note>{view.frames}</Note>
          </OverrideBanner>
        );
      })}
      {places.length === 0 ? (
        <EmptyState>
          {"No Frame yet. Each Frame you add shows here with its status. "}
          <Link href={WALL_HREF}>Add your first Frame on the Wall</Link>
        </EmptyState>
      ) : home.walls.map((wall) => {
        const strip = nowNextWords(home.now_next.find((entry) => entry.surface_id === wall.surface_id), now);
        return (
          <WallMap key={wall.surface_id} name={wall.name} room={wall.room} frames={wallFrames(wall, props, stale)}
            below={<NowNextStrip now={strip.now} next={strip.next} />} />
        );
      })}
      <Inline label="Quick actions">
        <WideOnly>
          <Button variant="primary" nativeButton={false} render={<a href={SHOW_NOW_HREF} />}>Show now</Button>
        </WideOnly>
        <Button disabled={busy} onClick={() => onHold("off")}>{POWER.allOff}</Button>
        <Button disabled={busy} onClick={() => onHold("on")}>{POWER.allOn}</Button>
        {said !== null && <Note live>{said}</Note>}
      </Inline>
    </Stack>
  );
}

/** The house banner while Central does not answer: what is shown is the last read, or nothing. */
function CentralDidNotAnswer({ since }: { since: string | null }) {
  return (
    <ProblemCard scope="central" verdict={houseVerdict(null)}
      what={since === null ? "Central did not answer, so nothing can be said about the house."
        : `Central did not answer. This is the house as last read at ${since}.`}
      doing="Photo Wall keeps trying." />
  );
}
