import { useEffect, useRef, useState } from "react";

import { BindingFacet } from "../BindingFacet.jsx";
import { FrameOverview } from "../domain/frame-overview";
import { FramePicture } from "../domain/frame-picture";
import { FramePosition, type PixelSize } from "../domain/frame-position";
import { FrameProfile, type Readiness } from "../domain/frame-profile";
import type { HostsRead } from "../domain/hosts-read";
import { frameHealth } from "../health.js";
import { boundOutput, isBound } from "../join.js";
import { useFrameAdjustment } from "../liveAdjustment.js";
import { EntityHeader } from "../patterns/entity-header";
import { EmptyState, EntityPage } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { HealthBadge } from "../patterns/health-badge";
import { LeaveGuard } from "../patterns/leave-guard";
import { playerPageHref } from "../players.js";
import { formatRoute, routeIdName, type Tab } from "../routes.js";
import { Tabs } from "../ui/tabs";

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
  surface_id: string;
  generation?: number;
  player_id?: string | null;
  output_id?: string | null;
  profile?: { width_px?: number; height_px?: number };
  calibration?: { rotation?: number };
  readiness?: Readiness;
}

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "position", label: "Position" },
  { value: "picture", label: "Picture" },
  { value: "hardware", label: "Hardware" },
] as const;

// A Pi that reports no output size and a Frame with no profile: steps are still pixels of a
// common output.
const FALLBACK_SIZE: PixelSize = { width: 1920, height: 1080 };

/** The output's size in pixels, as the Pi reported it, else the Frame profile's as mounted. */
function outputSize(snapshot: object | null, frame: Frame): PixelSize {
  const reported = (boundOutput(snapshot ?? {}, frame.id) as { observation?: { connected?: boolean; width_px?: number; height_px?: number } } | null)
    ?.observation;
  if (reported?.connected === true && (reported.width_px ?? 0) > 0 && (reported.height_px ?? 0) > 0) {
    return { width: reported.width_px!, height: reported.height_px! };
  }
  const { width_px: width = 0, height_px: height = 0 } = frame.profile ?? {};
  if (width <= 0 || height <= 0) return FALLBACK_SIZE;
  const rotation = frame.calibration?.rotation ?? 0;
  return rotation === 90 || rotation === 270 ? { width: height, height: width } : { width, height };
}

/**
 * One Frame's page (`#/wall/frames/<id>/<tab>`): every setting of one place on the wall, one
 * tab each. The header names the Frame, its way back to the Wall and its health (health.js,
 * the plan tile's words). Overview is what Central puts on it and why; Position is where the
 * picture sits on the Display (corners, nudges, trims, rotation); Picture is its brightness;
 * Hardware is which Pi and HDMI output feed it (bind, unbind, Identify) and its Frame profile.
 *
 * Position and Picture share one live adjustment (liveAdjustment.js): it runs while either tab
 * is shown, so moving between them keeps the change on the Display, and leaving them (another
 * tab, another page) ends it. Leaving them for another tab with changes not kept asks first
 * (LeaveGuard: Keep, Revert or Stay); leaving the page reverts them, and the page says so on
 * the next visit. Only the shown tab's content is mounted. The Frame's name is the page's
 * heading (the Wall route's `ownsHeading`).
 */
export function FramePage({ snapshot, bootFacts, hosts, frameId, tab, onTab, focusRequest, onFocusDone }: FramePageProps) {
  const frames = ((snapshot as { inventory?: { frames?: Frame[] } } | null)?.inventory?.frames ?? []);
  const frame = frames.find((candidate) => candidate.id === frameId);
  const live = tab === "position" || tab === "picture";
  const adjustment = useFrameAdjustment({ frameId, frame, open: live });
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  // A tab change that would leave a live adjustment with changes not kept: asked first.
  const [leaving, setLeaving] = useState<Tab | null>(null);
  const choose = (next: Tab) => {
    const stays = next === "position" || next === "picture";
    if (live && !stays && adjustment.dirty) setLeaving(next);
    else onTab(next);
  };
  const leave = async (keep: boolean) => {
    const target = leaving;
    if (target === null) return;
    const left = keep ? await adjustment.done() : (await adjustment.revert(), true);
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
  const health = frameHealth(snapshot, frameId) as { severity: "ok" | "todo" | "alarm"; label: string } | null;
  const softwareHref = isBound(frame) ? playerPageHref(snapshot, frame.player_id as string) : null;
  return (
    <EntityPage header={
      <EntityHeader title={`Frame ${frameId}`} level={1} headingRef={headingRef} back={back}>
        {health !== null && <HealthBadge verdict={{ severity: health.severity, label: health.label, receipt: null }} />}
        {walls.size > 1 && <Note>{`On the “${frame.surface_id}” wall`}</Note>}
      </EntityHeader>
    }>
      <LeaveGuard
        open={leaving !== null}
        busy={adjustment.busy}
        keepBlocked={adjustment.phase === "shown" ? null : "Keep is offered once the Pi presents your latest change."}
        onKeep={() => { void leave(true); }}
        onRevert={() => { void leave(false); }}
        onStay={() => setLeaving(null)}
      />
      <Tabs label="Frame settings" tabs={TABS} value={tab} onValueChange={(next) => choose(next as Tab)}>
        {tab === "overview" && <FrameOverview snapshot={snapshot} bootFacts={bootFacts} frameId={frameId} hosts={hosts} />}
        {tab === "position" && (
          <FramePosition adjustment={adjustment} size={outputSize(snapshot, frame)} softwareHref={softwareHref} />
        )}
        {tab === "picture" && <FramePicture adjustment={adjustment} softwareHref={softwareHref} />}
        {tab === "hardware" && (
          <>
            <BindingFacet key={frameId} snapshot={snapshot} bootFacts={bootFacts} frameId={frameId}
              onTab={(next: string) => choose(next as Tab)} />
            <FrameProfile key={`${frameId}:profile`} snapshot={snapshot} frame={frame} />
          </>
        )}
      </Tabs>
    </EntityPage>
  );
}
