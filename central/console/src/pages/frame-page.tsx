import { useCallback, useEffect, useRef, useState } from "react";

import { BindingFacet } from "../BindingFacet.jsx";
import { FrameDisplay } from "../domain/frame-display";
import { frameChip, type FrameHealth } from "../domain/frame-health-chip";
import { FrameOverview } from "../domain/frame-overview";
import { FrameLightingTab } from "../domain/frame-lighting/frame-lighting-tab";
import type { PaperProfileChoice } from "../domain/frame-lighting/model";
import { FrameDraftFit, FramePhotos } from "../domain/frame-photos";
import { listPaperProfiles } from "../domain/paper-profiles/api";
import { FramePicture } from "../domain/frame-picture";
import { PictureHistory } from "../domain/picture-history";
import { PositionHistory } from "../domain/position-history";
import { FramePosition, type PixelSize } from "../domain/frame-position";
import { FrameInput, FramePower } from "../domain/frame-power";
import { useFrameWhy } from "../domain/frame-why";
import { FrameProfile, type Readiness } from "../domain/frame-profile";
import { FrameTurn } from "../domain/frame-turn";
import type { Turn } from "../domain/turn-words";
import type { HostsRead } from "../domain/hosts-read";
import { readDrawn, readRoomLights } from "../domain/room-lights/api";
import { DrawnLine } from "../domain/room-lights/drawn-line";
import { drawnOf, lightNames } from "../domain/room-lights/model";
import type { DrawnView, RoomLightsView } from "../generated/contract";
import { frameHealth } from "../health.js";
import { isBound } from "../join.js";
import { useFrameAdjustment } from "../liveAdjustment.js";
import { renameFrame } from "../framesApi.js";
import { EntityHeader } from "../patterns/entity-header";
import { EmptyState, EntityPage } from "../patterns/entity-page";
import { LeaveGuard } from "../patterns/leave-guard";
import { usePictureSession } from "../pictureSession.js";
import { useMutate } from "../useMutate.js";
import { playerPageHref } from "../players.js";
import { usePolledRead } from "../polledRead.js";
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
  /** How the Frame's screen hangs (Central's `turn`; set on the Hardware tab). */
  turn?: Turn;
  /** The Frame's screen, the mode as built (Central's `screen_px`); null when not known. */
  screen_px?: [number, number] | null;
  /** The Display seen there's own size in millimetres (its EDID's); null when none is stated. */
  display_mm?: [number, number] | null;
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
  { value: "lighting", label: "Lighting" },
  { value: "picture", label: "Picture" },
  { value: "power", label: "Power" },
  { value: "hardware", label: "Hardware" },
] as const;

/** The Frame's screen size as History says an entry's box in it; null when not known. */
function screenSize(frame: Frame): PixelSize | null {
  return frame.screen_px == null ? null : { width: frame.screen_px[0], height: frame.screen_px[1] };
}

/** How often the house's lights are read while the Lighting or Position tab is shown: a light's
 * state and a Sensor's reading stay current beside the light that views it. */
const HOUSE_CADENCE_MS = 5000;
// The tabs that share the Frame's live session (Position's and the lighting's).
const SESSION_TABS: readonly Tab[] = ["position", "lighting"];

/**
 * What the Frame's lighting chooses from and its history names, while `open`: the house's room
 * lights and Sensors, read every HOUSE_CADENCE_MS (a failed read keeps the last); what each Pi draws
 * with, read as often (a failed read shows nothing: an old "Up to date" would hide an outage); and
 * the paper profiles on the list, read once each time it opens. Null: not read yet.
 */
function useHouseLights(open: boolean): { rooms: RoomLightsView | null; drawn: DrawnView | null;
  profiles: PaperProfileChoice[] | null } {
  const loadRooms = useCallback(async (previous: RoomLightsView | null) => {
    const answer = await readRoomLights();
    return answer.ok ? answer.view : previous;
  }, []);
  const loadDrawn = useCallback(async () => {
    const answer = await readDrawn();
    return answer.ok ? answer.drawn : null;
  }, []);
  const { value: rooms } = usePolledRead(loadRooms, { cadenceMs: HOUSE_CADENCE_MS, skip: !open, initial: null as RoomLightsView | null });
  const { value: drawn } = usePolledRead(loadDrawn, { cadenceMs: HOUSE_CADENCE_MS, skip: !open, initial: null as DrawnView | null });
  const [profiles, setProfiles] = useState<PaperProfileChoice[] | null>(null);
  useEffect(() => {
    if (!open) return undefined;
    let current = true;
    void listPaperProfiles().then((result) => {
      if (current && result.ok) {
        setProfiles(result.view.paper_profiles.map(({ profile_sha256, recipe_id, name }) => ({ profile_sha256, recipe_id, name })));
      }
    });
    return () => { current = false; };
  }, [open]);
  return { rooms, drawn, profiles };
}

/**
 * One Frame's page (`#/wall/frames/<id>/<tab>`): every setting of one place on the wall, one
 * tab each. The header names the Frame, its way back to the Wall and its health (health.js,
 * the plan tile's words). Overview is what Central puts on it and why, and its Photos: one Fit board
 * row per Scene that shows photos here (follow-ups C1); Position is where the
 * picture sits on its screen as it hangs (one box: move, width and height, nudges), with the Frame's Position
 * History; Lighting is the room lights that fall on it, its mat and its paper, and whether its Pi
 * draws with the house's lights as they are now, with the same History; Picture is
 * the contrast, colour temperature, gamma and matched light of the display that feeds it, with that display's History
 * (every kept and tried change, and Restore into the editor); Power is how the Pi turns
 * its display off and on, with Test: turn off and Turn on; Hardware is which Pi and HDMI output
 * feed it (bind, unbind, Identify), the Display the Pi reports there (with the display-changed
 * card), its Frame profile and how its screen hangs (the turn).
 *
 * Position and Lighting share one live session (liveAdjustment.js, kept on the Frame), Picture has
 * its own (pictureSession.js, kept on the display). Each runs while one of its tabs is shown and
 * ends when they are left; moving between Position and Lighting keeps it. Leaving either tab with changes not kept asks first
 * (LeaveGuard: Keep, Revert or Stay); leaving the page reverts them, and the tab says so on the
 * next visit. Only the shown tab's content is mounted. The Frame's name is the page's heading
 * (the Wall route's `ownsHeading`).
 */
export function FramePage({ snapshot, bootFacts, hosts, frameId, tab, onTab, focusRequest, onFocusDone }: FramePageProps) {
  const frames = ((snapshot as { inventory?: { frames?: Frame[] } } | null)?.inventory?.frames ?? []);
  const frame = frames.find((candidate) => candidate.id === frameId);
  const session = SESSION_TABS.includes(tab);
  const adjustment = useFrameAdjustment({ frameId, frame, open: session });
  // The Frame's lighting chooses among the house's room lights and paper profiles; its history names them.
  const { rooms, drawn, profiles } = useHouseLights(session && frame !== undefined);
  // The Frame's Why, read once for the page: the header's Resting, Overview's sentence, Power's line.
  const { read: why, refresh: rereadWhy } = useFrameWhy(frameId);
  // A tab shown reads Why again: what it says is as of now, not the last poll's.
  useEffect(() => { void rereadWhy(); }, [tab, rereadWhy]);
  const mutate = useMutate() as <T>(op: () => Promise<T>) => Promise<T>;
  const picture = usePictureSession({ frameId, open: tab === "picture" && frame !== undefined });
  // The shown tab's live session, if it has one: the one a tab change would leave.
  const guarded = session ? adjustment : tab === "picture" ? picture : null;
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  // A tab change that would leave a live session with changes not kept: asked first.
  const [leaving, setLeaving] = useState<Tab | null>(null);
  const choose = (next: Tab) => {
    const sameSession = session && SESSION_TABS.includes(next);
    if (guarded !== null && next !== tab && !sameSession && guarded.dirty) setLeaving(next);
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
          <FramePosition adjustment={adjustment} size={frame.screen_px ?? null} displayMm={frame.display_mm ?? null}
            piHref={piHref} turn={frame.turn ?? 0}
            hardwareHref={formatRoute({ section: "wall", id: frameId, tab: "hardware" })}
            readiness={frame.readiness}
            whatFits={(
              <FrameDraftFit snapshot={snapshot} frameId={frameId} profile={wholeProfile(frame.profile)}
                calibration={{ revision: frame.calibration?.revision ?? 1, fit: adjustment.draft.fit }} />
            )} />
        )}
        {tab === "lighting" && (
          <FrameLightingTab adjustment={adjustment} piHref={piHref} rooms={rooms} profiles={profiles}
            drawn={isBound(frame) && drawn !== null
              ? <DrawnLine drawn={drawnOf(drawn.players, frame.player_id as string)} /> : undefined} />
        )}
        {session && (
          // The Frame's Position history (its fit and its lighting), with Restore into the draft.
          <PositionHistory key={`${frameId}:position-history`} frameId={frameId} adjustment={adjustment}
            size={screenSize(frame)}
            hardwareHref={formatRoute({ section: "wall", id: frameId, tab: "hardware" })}
            roomNames={lightNames(rooms)}
            profiles={profiles ?? []} />
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
                  calibration={{ revision: frame.calibration?.revision ?? 1, fit: frame.calibration?.fit ?? { mode: "box" } }} />
              )} />
            <FrameTurn key={`${frameId}:turn`} frameId={frameId} turn={frame.turn ?? 0}
              generation={frame.generation ?? 0} />
          </>
        )}
      </Tabs>
    </EntityPage>
  );
}
