import { useMemo } from "react";

import { isBound, plannedFor, rankedContributions } from "../join.js";
import { EmptyState } from "../patterns/entity-page";
import { OwnerLinks } from "../patterns/link-to-owner";
import { formatRoute, sceneCreationRoute } from "../routes.js";
import { windowWords } from "../timeWords.js";
import { Section } from "../ui/section";
import { Stack } from "../ui/stack";
import { FitBoardFor, type FitQuestion } from "./photo-fit-board";
import { type FrameDraft, type JudgedAt, type SceneUse, scenesOnFrame } from "./photo-fit";

interface Served {
  runtime?: {
    current?: { now?: number };
    programs?: Record<string, { program_id: string; scene_id: string }>;
    program_windows?: Record<string, { next: { start_at: number; start: string; end: string } | null }>;
  } | null;
  inventory?: { frames?: { id: string; player_id?: string | null; output_id?: string | null }[] };
}

/**
 * How a Scene relates to now on this Frame, in words: "Playing now", "Waiting underneath what
 * plays now", "Scheduled Mon 12 Oct 17:00–23:00" (the soonest next window of a Program playing it,
 * on the house clock), or "Not scheduled". Read from Central's Runtime: Central works out every
 * window; the console compares no times.
 */
export function relationWords(snapshot: object | null, frameId: string, sceneId: string): string {
  const served = snapshot as Served | null;
  const frame = (served?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const runtime = served?.runtime ?? {};
  if (plannedFor(runtime as object, frameId, isBound(frame)).sceneId === sceneId) return "Playing now";
  if ((rankedContributions(runtime as object, frameId) as { scene_id: string }[]).some((entry) => entry.scene_id === sceneId)) {
    return "Waiting underneath what plays now";
  }
  const next = Object.values(runtime?.programs ?? {})
    .filter((program) => program.scene_id === sceneId)
    .map((program) => runtime?.program_windows?.[program.program_id]?.next ?? null)
    .filter((window) => window !== null)
    .sort((a, b) => a!.start_at - b!.start_at)[0];
  return next === undefined || next === null ? "Not scheduled" : `Scheduled ${windowWords(next)}`;
}

function SceneRow({ snapshot, frameId, scene }: { snapshot: object | null; frameId: string; scene: SceneUse }) {
  const question: FitQuestion = useMemo(() => ({
    selection: { sources: scene.sourceRefs }, frames: [frameId], crop_limit: scene.cropLimit,
  }), [scene, frameId]);
  // One line for what the Scene holds and what it is judged at (the board leads with the picture).
  const judged: JudgedAt = { at: "frame-scene", ownCrop: scene.cropLimit !== null };
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
  /** Its Position as it would be (the draft, or the saved one), the one source of its shape and
   * size; null while not known. */
  calibration: object | null;
}

/**
 * The Frame config moment (follow-ups C1): while a Frame's Position is being changed,
 * one line per Scene that shows photos here, judged with the unsaved values (a `FrameDraft`), so
 * the change's effect on what fits shows before it is kept. Nothing when no Scene shows photos
 * here or the values are not whole yet.
 */
export function FrameDraftFit({ snapshot, frameId, calibration }: FrameDraftFitProps) {
  const scenes = scenesOnFrame(snapshot, frameId);
  if (scenes.length === 0 || calibration === null) return null;
  return (
    <Stack label="What would fit">
      {scenes.map((scene) => (
        <DraftLine key={scene.sceneId} scene={scene} frameId={frameId} calibration={calibration} />
      ))}
    </Stack>
  );
}

function DraftLine({ scene, frameId, calibration }: {
  scene: SceneUse; frameId: string; calibration: object;
}) {
  const key = JSON.stringify(calibration);
  const question: FitQuestion = useMemo(() => {
    const draft: FrameDraft = { frame_id: frameId, calibration: JSON.parse(key) as object };
    return {
      selection: { sources: scene.sourceRefs }, frames: [frameId], crop_limit: scene.cropLimit,
      frame_drafts: [draft],
    };
  }, [scene, frameId, key]);
  return (
    <FitBoardFor question={question} size="line" lineOutput named={false}
      title={`Scene ${scene.sceneId} on this Frame, as you are setting it`} />
  );
}
