import { useMemo } from "react";

import { isBound, plannedFor, rankedContributions } from "../join.js";
import { EmptyState } from "../patterns/entity-page";
import { OwnerLinks } from "../patterns/link-to-owner";
import { formatRoute, sceneCreationRoute } from "../routes.js";
import { countWords, windowLabel } from "../timeWords.js";
import { Section } from "../ui/section";
import { Stack } from "../ui/stack";
import { FitBoardFor, type FitQuestion } from "./photo-fit-board";
import { type FitAnalysis, type FrameDraft, percentWords, type SceneUse, scenesOnFrame } from "./photo-fit";

interface Served {
  runtime?: {
    current?: { now?: number };
    programs?: Record<string, { program_id: string; scene_id: string; starts_at: number; ends_at: number }>;
  } | null;
  inventory?: { frames?: { id: string; player_id?: string | null; output_id?: string | null }[] };
}

/**
 * How a Scene relates to now on this Frame, in words: "Playing now", "Waiting underneath what
 * plays now", "Scheduled Tue 2 Mar 17:00–23:00 BST" (its next window), or "Not scheduled". Read
 * from Central's Runtime (its own clock: the windows are compared with `current.now`).
 */
export function relationWords(snapshot: object | null, frameId: string, sceneId: string): string {
  const served = snapshot as Served | null;
  const frame = (served?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const runtime = served?.runtime ?? {};
  if (plannedFor(runtime as object, frameId, isBound(frame)).sceneId === sceneId) return "Playing now";
  if ((rankedContributions(runtime as object, frameId) as { scene_id: string }[]).some((entry) => entry.scene_id === sceneId)) {
    return "Waiting underneath what plays now";
  }
  const now = runtime?.current?.now ?? null;
  const next = Object.values(runtime?.programs ?? {})
    .filter((program) => program.scene_id === sceneId && (now === null || program.ends_at > now))
    .sort((a, b) => a.starts_at - b.starts_at)[0];
  return next === undefined ? "Not scheduled" : `Scheduled ${windowLabel(next)}`;
}

function SceneRow({ snapshot, frameId, scene }: { snapshot: object | null; frameId: string; scene: SceneUse }) {
  const question: FitQuestion = useMemo(() => ({
    selection: { sources: scene.sourceRefs }, frames: [frameId], crop_limit: scene.cropLimit,
  }), [scene, frameId]);
  // One line for what the Scene holds and what it is judged at (the board leads with the picture).
  const judged = (analysis: FitAnalysis) => `From the Scene's ${countWords(analysis.photos, "photo")}${
    analysis.videos > 0 ? ` and ${countWords(analysis.videos, "video")}` : ""}, at ${
    scene.cropLimit === null ? "the house crop limit" : "its own crop limit"}, ${percentWords(analysis.crop_limit)}.`;
  return (
    <Stack label={`Scene ${scene.sceneId}`}>
      <OwnerLinks label={`Scene “${scene.sceneId}” · ${relationWords(snapshot, frameId, scene.sceneId)}`} links={[
        { text: "Edit Scene", href: formatRoute({ section: "scenes", id: scene.sceneId, flow: "edit", step: "review" }) },
      ]} />
      <FitBoardFor question={question} size="full" lead="frame" named={false} judged={judged}
        title={`What this Frame gets from Scene ${scene.sceneId}`} />
    </Stack>
  );
}

/**
 * The Frame page's Photos (Overview; follow-ups C1, replacing 5a's three lines of counts): one
 * row per Scene that shows photos here, with how it relates to now and a link to the Scene, and
 * what this Frame gets from it (design review round 2): led by this Frame's tile, filled with
 * what it would show, and the photos it would show next, cropped as it crops them; then how many
 * of the Scene's photos fit and why the rest do not, judged at the Scene's crop limit.
 */
export function FramePhotos({ snapshot, frameId }: { snapshot: object | null; frameId: string }) {
  const scenes = scenesOnFrame(snapshot, frameId);
  return (
    <Section title="Photos">
      {scenes.length === 0 ? (
        <>
          <EmptyState>No Scene shows photos from a Source on this Frame yet.</EmptyState>
          <OwnerLinks links={[{ text: "Put content on this Frame", href: formatRoute(sceneCreationRoute(frameId) as never) }]} />
        </>
      ) : scenes.map((scene) => <SceneRow key={scene.sceneId} snapshot={snapshot} frameId={frameId} scene={scene} />)}
    </Section>
  );
}

export interface FrameDraftFitProps {
  snapshot: object | null;
  frameId: string;
  /** The Frame profile as it would be (the draft, or the saved one); null while not whole. */
  profile: FrameDraft["profile"] | null;
  /** Its Position as it would be (the draft, or the saved one); null while not known. */
  calibration: object | null;
}

/**
 * The Frame config moment (follow-ups C1): while a Frame profile or Position is being changed,
 * one line per Scene that shows photos here, judged with the unsaved values (a `FrameDraft`), so
 * the change's effect on what fits shows before it is kept. Nothing when no Scene shows photos
 * here or the values are not whole yet.
 */
export function FrameDraftFit({ snapshot, frameId, profile, calibration }: FrameDraftFitProps) {
  const scenes = scenesOnFrame(snapshot, frameId);
  if (scenes.length === 0 || profile === null || calibration === null) return null;
  return (
    <Stack label="What would fit">
      {scenes.map((scene) => (
        <DraftLine key={scene.sceneId} scene={scene} frameId={frameId} profile={profile} calibration={calibration} />
      ))}
    </Stack>
  );
}

function DraftLine({ scene, frameId, profile, calibration }: {
  scene: SceneUse; frameId: string; profile: FrameDraft["profile"]; calibration: object;
}) {
  const key = JSON.stringify([profile, calibration]);
  const question: FitQuestion = useMemo(() => {
    const [draftProfile, draftCalibration] = JSON.parse(key) as [FrameDraft["profile"], object];
    return {
      selection: { sources: scene.sourceRefs }, frames: [frameId], crop_limit: scene.cropLimit,
      frame_drafts: [{ frame_id: frameId, profile: draftProfile, calibration: draftCalibration }],
    };
  }, [scene, frameId, key]);
  return (
    <FitBoardFor question={question} size="line" lineOutput named={false}
      title={`Scene ${scene.sceneId} on this Frame, as you are setting it`} />
  );
}
