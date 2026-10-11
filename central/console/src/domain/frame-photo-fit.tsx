import { useCallback } from "react";

import { readCandidates } from "../candidatesApi.js";
import { frameOf } from "../join.js";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { usePolledRead } from "../polledRead.js";
import { sourceName } from "../sourceNames.js";
import { Section } from "../ui/section";

/**
 * Why Central leaves a photo off a Frame (central/wall_plan/fit.py `FitRefusal`), plus
 * `not_measured`: a photo it lists that is not measured yet (central/media_repository.py
 * `source_candidates`).
 */
export type Skip =
  | "beyond_crop_limit"
  | "below_frame_pixels"
  | "face_cut"
  | "interest_cut"
  | "no_video_here"
  | "not_measured";

/** Each reason in the page's words, in the order the page lists them. No orientation words:
 * fit is about a photo's ratio, never a landscape or portrait label (roadmap 5a). */
export const SKIP_WORDS: Readonly<Record<Skip, string>> = {
  beyond_crop_limit: "doesn't fit this Frame's shape",
  below_frame_pixels: "too small for this Frame",
  face_cut: "a face would be cut",
  interest_cut: "the interesting part would be cut",
  no_video_here: "this Frame shows no videos",
  not_measured: "not measured yet",
};

const SKIPS = Object.keys(SKIP_WORDS) as Skip[];

/** One Source a Scene shows on this Frame, and the crop limit that Scene judges its photos at. */
export interface PhotoSource {
  sceneId: string;
  sourceRef: string;
  /** The Scene's crop limit, a fraction; null when the stored Scene names none. */
  cropLimit: number | null;
}

/** What Central answered for one Source on this Frame. */
export type PhotoFitRead =
  | { status: "loading" }
  | { status: "failed"; error: string }
  | { status: "read"; ready: boolean; fits: number; skipped: Partial<Record<Skip, number>> };

export interface PhotoFitEntry extends PhotoSource {
  read: PhotoFitRead;
}

interface StoredContribution {
  target?: string;
  source_refs?: string[];
}

interface StoredScene {
  scene_id?: string;
  selection?: { crop_limit?: number };
  contributions?: StoredContribution[];
  outro_contributions?: StoredContribution[];
  children?: { scene?: StoredScene }[];
}

/**
 * The Sources the stored Scenes show on `frameId` (the snapshot's runtime document, its
 * `definitions`), each with the crop limit of the Scene that names it: a contribution of the
 * Scene's body or outro, or of an inline child Scene (judged at the child's own limit). One
 * entry per Scene and Source, sorted by Scene, then Source.
 */
export function frameSources(snapshot: object | null, frameId: string): PhotoSource[] {
  const definitions = (snapshot as { runtime?: { definitions?: Record<string, StoredScene> } } | null)
    ?.runtime?.definitions ?? {};
  const found = new Map<string, PhotoSource>();
  const visit = (scene: StoredScene | undefined, sceneId: string) => {
    if (scene === undefined) return;
    const cropLimit = typeof scene.selection?.crop_limit === "number" ? scene.selection.crop_limit : null;
    for (const entry of [...(scene.contributions ?? []), ...(scene.outro_contributions ?? [])]) {
      if (frameOf(entry.target ?? "") !== frameId) continue;
      for (const sourceRef of entry.source_refs ?? []) {
        found.set(`${sceneId}\n${sourceRef}`, { sceneId, sourceRef, cropLimit });
      }
    }
    for (const child of scene.children ?? []) visit(child.scene, child.scene?.scene_id ?? sceneId);
  };
  for (const [sceneId, scene] of Object.entries(definitions)) visit(scene, scene.scene_id ?? sceneId);
  return [...found.values()].sort((a, b) =>
    a.sceneId === b.sceneId ? a.sourceRef.localeCompare(b.sourceRef) : a.sceneId.localeCompare(b.sceneId));
}

function fitWords(fits: number): string {
  return `${fits} ${fits === 1 ? "photo fits" : "photos fit"} this Frame`;
}

function PhotoFitGroup({ entry }: { entry: PhotoFitEntry }) {
  const { read } = entry;
  const limit = entry.cropLimit === null ? "" : `, crop limit ${Math.round(entry.cropLimit * 100)} %`;
  return (
    <FactGroup title={`Scene “${entry.sceneId}” from Source “${sourceName(entry.sourceRef)}”${limit}`}>
      {read.status === "loading" && <Note>Reading its photos…</Note>}
      {read.status === "failed" && <Note>{`Could not read its photos for this Frame: ${read.error}.`}</Note>}
      {read.status === "read" && (
        <>
          <FactRow tone="derived">{fitWords(read.fits)}</FactRow>
          {SKIPS.filter((skip) => (read.skipped[skip] ?? 0) > 0).map((skip) => (
            <FactRow key={skip} tone="derived" label={SKIP_WORDS[skip]}>{String(read.skipped[skip])}</FactRow>
          ))}
          {!read.ready && <Note>The Source&apos;s first check is not finished, so more photos may come.</Note>}
        </>
      )}
    </FactGroup>
  );
}

/**
 * The Frame page's Photos section (roadmap 5a, run ledger K2), given what Central answered: for
 * each Source a Scene shows here, how many of its photos fit this Frame, then one line per reason
 * Central leaves the others off, each with its count (a reason with none is left out).
 */
export function PhotoFitPanel({ entries }: { entries: PhotoFitEntry[] }) {
  return (
    <Section title="Photos">
      {entries.length === 0
        ? <Note>No Scene shows photos from a Source on this Frame.</Note>
        : entries.map((entry) => <PhotoFitGroup key={`${entry.sceneId}\n${entry.sourceRef}`} entry={entry} />)}
    </Section>
  );
}

/** How often the counts are read again while the Overview is shown: new photos are measured
 * within seconds of a check (run ledger F-10), and a check may add photos. */
const CADENCE_MS = 15_000;

interface Reads {
  key: string;
  reads: PhotoFitRead[];
}

const NOT_READ: Reads = { key: "", reads: [] };

/**
 * The Frame page's Photos, read: the Sources of the Scenes that target this Frame
 * ({@link frameSources}), each read from Central's candidates route at its Scene's crop limit
 * (candidatesApi.js `readCandidates`), again every {@link CADENCE_MS} while shown.
 */
export function FramePhotoFit({ snapshot, frameId }: { snapshot: object | null; frameId: string }) {
  const sources = frameSources(snapshot, frameId);
  const key = JSON.stringify([frameId, sources]);
  const load = useCallback(async (): Promise<Reads> => {
    const wanted = JSON.parse(key)[1] as PhotoSource[];
    const reads = await Promise.all(wanted.map(async (source): Promise<PhotoFitRead> => {
      try {
        const answer = await readCandidates(source.sourceRef, frameId, source.cropLimit ?? undefined);
        const skipped = answer.skipped as Partial<Record<Skip, number>>;
        return { status: "read", ready: answer.ready,
          fits: answer.candidates.length - (skipped.not_measured ?? 0), skipped };
      } catch (error) {
        return { status: "failed", error: error instanceof Error ? error.message : "the request did not complete" };
      }
    }));
    return { key, reads };
  }, [key, frameId]);
  const { value } = usePolledRead(load, { cadenceMs: CADENCE_MS, skip: sources.length === 0, initial: NOT_READ });
  // A read of other Sources (the Scenes changed meanwhile) is never shown as these.
  const current = value.key === key;
  const entries = sources.map((source, index): PhotoFitEntry => ({
    ...source, read: current ? value.reads[index] : { status: "loading" },
  }));
  return <PhotoFitPanel entries={entries} />;
}
