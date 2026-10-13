import { useEffect, useRef, useState } from "react";

import { BindingFacet } from "../BindingFacet.jsx";
import { FrameDisplay } from "../domain/frame-display";
import { frameChip, type FrameHealth } from "../domain/frame-health-chip";
import { FrameOverview } from "../domain/frame-overview";
import { FrameDraftFit, FramePhotos } from "../domain/frame-photos";
import { FramePicture } from "../domain/frame-picture";
import { PictureHistory } from "../domain/picture-history";
import { FramePosition, type PixelSize } from "../domain/frame-position";
import { FrameInput, FramePower } from "../domain/frame-power";
import { useFrameWhy } from "../domain/frame-why";
import { FrameProfile, type Readiness } from "../domain/frame-profile";
import type { HostsRead } from "../domain/hosts-read";
import { frameHealth } from "../health.js";
import { boundOutput, isBound } from "../join.js";
import { useFrameAdjustment } from "../liveAdjustment.js";
import { renameFrame } from "../framesApi.js";
import { EntityHeader } from "../patterns/entity-header";
import { EmptyState, EntityPage } from "../patterns/entity-page";
import { LeaveGuard } from "../patterns/leave-guard";
import { usePictureSession } from "../pictureSession.js";
import { useMutate } from "../useMutate.js";
import { playerPageHref } from "../players.js";
import { formatRoute, routeIdName, type Tab } from "../routes.js";
import { Tabs } from "../ui/tabs";
import { FramePageHeader, type RenameAnswer } from "./frame-page-header";

export interface FramePageProps {
  snapshot: object | null;
  bootFacts: object | null;
  hosts: HostsRead | null;
  frameId: string;
  tab: Tab;
  onTab: (tab: Tab) => void;
  /** A visit's request to move focus to the page's heading (wallState.js), spent once. */
  focusRequest: number | null;
  onFocusDone: () => void;
}

interface Frame {
  id: string;
  name?: string;
  surface_id: string;
  generation?: number;
  player_id?: string | null;
  output_id?: string | null;
  profile?: { width_px?: number; height_px?: number; diagonal_inches?: number; video?: boolean };
  calibration?: { revision?: number; fit?: object };
  readiness?: Readiness;
}

type WholeProfile = { width_px: number; height_px: number; diagonal_inches: number; video: boolean };

/** A Frame profile whose every value is set, as a Frame draft carries it; null otherwise. */
function wholeProfile(profile: Frame["profile"] | null | undefined): WholeProfile | null {
  if (!profile) return null;
  const { width_px: width, height_px: height, diagonal_inches: diagonal } = profile;
  if (!(Number.isInteger(width) && (width ?? 0) > 0 && Number.isInteger(height) && (height ?? 0) > 0
    && Number.isFinite(diagonal) && (diagonal ?? 0) > 0)) return null;
  return { width_px: width!, height_px: height!, diagonal_inches: diagonal!, video: profile.video ?? true };
}

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "position", label: "Position" },
  { value: "picture", label: "Picture" },
  { value: "power", label: "Power" },
  { value: "hardware", label: "Hardware" },
] as const;

/**
 * The output's size in pixels, as the Pi reported it; null when it reported none. Position's
 * steps and corner pixels are pixels of the output, whatever way the picture is turned in it.
 * Never the Frame profile: that is the Frame's shape, not the output's (a Frame may fill part
 * of its output, or be hung turned in it).
 */
function outputSize(snapshot: object | null, frame: Frame): PixelSize | null {
  const reported = (boundOutput(snapshot ?? {}, frame.id) as { observation?: { connected?: boolean; width_px?: number; height_px?: number } } | null)
    ?.observation;
  if (reported?.connected === true && (reported.width_px ?? 0) > 0 && (reported.height_px ?? 0) > 0) {
    return { width: reported.width_px!, height: reported.height_px! };
  }
  return null;
}

/**
 * One Frame's page (`#/wall/frames/<id>/<tab>`): every setting of one place on the wall, one
 * tab each. The header names the Frame, its way back to the Wall and its health (health.js,
 * the plan tile's words). Overview is what Central puts on it and why, and its Photos: one Fit board
 * row per Scene that shows photos here (follow-ups C1); Position is where the
 * picture sits on the Display (its fit, corners, nudges, rotation); Picture is the brightness,
 * contrast, colour temperature and gamma of the display that feeds it, with that display's History
 * (every kept and tried change, and Restore into the editor); Power is how the Pi turns
 * its display off and on, with Test: turn off and Turn on; Hardware is which Pi and HDMI output
 * feed it (bind, unbind, Identify), the Display the Pi reports there (with the display-changed
 * card) and its Frame profile.
 *
 * Position and Picture each have their own live session: Position's (liveAdjustment.js, kept on
 * the Frame) and Picture's (pictureSession.js, kept on the display). Each runs while its tab is
 * shown and ends when it is left. Leaving either tab with changes not kept asks first
 * (LeaveGuard: Keep, Revert or Stay); leaving the page reverts them, and the tab says so on the
 * next visit. Only the shown tab's content is mounted. The Frame's name is the page's heading
 * (the Wall route's `ownsHeading`).
 */
export function FramePage({ snapshot, bootFacts, hosts, frameId, tab, onTab, focusRequest, onFocusDone }: FramePageProps) {
  const frames = ((snapshot as { inventory?: { frames?: Frame[] } } | null)?.inventory?.frames ?? []);
  const frame = frames.find((candidate) => candidate.id === frameId);
  const adjustment = useFrameAdjustment({ frameId, frame, open: tab === "position" });
  // The Frame's Why, read once for the page: the header's Resting, Overview's sentence, Power's line.
  const { read: why, refresh: rereadWhy } = useFrameWhy(frameId);
  // A tab shown reads Why again: what it says is as of now, not the last poll's.
  useEffect(() => { void rereadWhy(); }, [tab, rereadWhy]);
  const mutate = useMutate() as <T>(op: () => Promise<T>) => Promise<T>;
  const picture = usePictureSession({ frameId, open: tab === "picture" && frame !== undefined });
  // The shown tab's live session, if it has one: the one a tab change would leave.
  const guarded = tab === "position" ? adjustment : tab === "picture" ? picture : null;
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  // A tab change that would leave a live session with changes not kept: asked first.
  const [leaving, setLeaving] = useState<Tab | null>(null);
  const choose = (next: Tab) => {
    if (guarded !== null && next !== tab && guarded.dirty) setLeaving(next);
    else onTab(next);
  };
  const leave = async (keep: boolean) => {
    const target = leaving;
    if (target === null || guarded === null) return;
    const left = keep ? await guarded.done() : (await guarded.revert(), true);
    setLeaving(null);
    if (left) onTab(target);
  };

  useEffect(() => {
    if (focusRequest === null || headingRef.current === null) return;
    headingRef.current.focus();
    onFocusDone();
  }, [focusRequest, frameId, onFocusDone]);

  const back = { text: "Wall", href: formatRoute({ section: "wall" }) };
  if (frame === undefined) {
    // A typed address may hold any text: the heading never echoes one that is no id.
    return (
      <EntityPage header={
        <EntityHeader title={routeIdName("Frame", frameId, { start: true })} level={1} headingRef={headingRef} back={back} />
      }>
        <EmptyState>{`${routeIdName("Frame", frameId, { start: true })}: This no longer exists.`}</EmptyState>
      </EntityPage>
    );
  }
  const walls = new Set(frames.map((candidate) => candidate.surface_id));
  const classified = frameHealth(snapshot, frameId) as FrameHealth | null;
  // Resting is Central's one answer (Why's `power.resting`), never worked out here.
  const resting = why !== null && why.ok && why.why.power?.resting === true;
  const health = classified === null ? null : frameChip(classified, false, resting);
  const piHref = isBound(frame) ? playerPageHref(snapshot, frame.player_id as string) : null;
  return (
    <EntityPage header={
      <FramePageHeader name={frame.name ?? frameId} chip={health} wall={walls.size > 1 ? frame.surface_id : null}
        back={back} headingRef={headingRef}
        onRename={(next) => mutate(() => renameFrame(frameId, next)) as Promise<RenameAnswer>} />
    }>
      <LeaveGuard
        open={leaving !== null}
        busy={guarded?.busy ?? false}
        keepBlocked={guarded?.phase === "shown" ? null : "Keep is offered once the Pi presents your latest change."}
        onKeep={() => { void leave(true); }}
        onRevert={() => { void leave(false); }}
        onStay={() => setLeaving(null)}
      />
      <Tabs label="Frame settings" tabs={TABS} value={tab} onValueChange={(next) => choose(next as Tab)}>
        {tab === "overview" && (
          <>
            <FrameOverview snapshot={snapshot} bootFacts={bootFacts} frameId={frameId} frameName={frame.name ?? frameId}
              hosts={hosts} why={why} />
            <FramePhotos key={`${frameId}:photos`} snapshot={snapshot} frameId={frameId} />
          </>
        )}
        {tab === "position" && (
          <FramePosition adjustment={adjustment} size={outputSize(snapshot, frame)} piHref={piHref}
            readiness={frame.readiness}
            whatFits={(
              <FrameDraftFit snapshot={snapshot} frameId={frameId} profile={wholeProfile(frame.profile)}
                calibration={{ revision: frame.calibration?.revision ?? 1, fit: adjustment.draft.fit }} />
            )} />
        )}
        {tab === "picture" && (
          <>
            <FramePicture frameId={frameId} piHref={piHref} session={picture}
              otherFrames={frames.filter((other) => other.id !== frameId).map((other) => other.id)} />
            {/* The display's input on both tabs (decision 0022 R10): Power's own row. */}
            <FrameInput key={`${frameId}:input`} frameId={frameId} />
            {/* The display's picture history, with Restore into the editor (configuration snapshots). */}
            {picture.view?.display_id != null && (
              <PictureHistory key={`${frameId}:history:${picture.view.display_id}`} displayId={picture.view.display_id}
                session={picture} />
            )}
          </>
        )}
        {tab === "power" && (
          <FramePower key={`${frameId}:power`} frameId={frameId} why={why}
            readAt={(snapshot as { readAt?: number } | null)?.readAt ?? null} />
        )}
        {tab === "hardware" && (
          <>
            <BindingFacet key={frameId} snapshot={snapshot} bootFacts={bootFacts} frameId={frameId}
              onTab={(next: string) => choose(next as Tab)} />
            <FrameDisplay key={`${frameId}:display`} frameId={frameId} />
            <FrameProfile key={`${frameId}:profile`} snapshot={snapshot} frame={frame}
              draftFit={(profile) => (
                <FrameDraftFit snapshot={snapshot} frameId={frameId} profile={wholeProfile(profile)}
                  calibration={{ revision: frame.calibration?.revision ?? 1, fit: frame.calibration?.fit ?? { mode: "cover" } }} />
              )} />
          </>
        )}
      </Tabs>
    </EntityPage>
  );
}
