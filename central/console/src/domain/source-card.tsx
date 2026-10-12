import { useEffect, useState } from "react";
import type * as React from "react";

import { type CardChoice, MediaCard } from "../patterns/media-card";
import type { MenuItem } from "../ui/menu";
import { sourcePool } from "../photoSourcesApi.js";
import { checkWords, counted, queryWords } from "../sourceWords.js";
import { captureDay } from "../timeWords.js";
import type { CheckState, PoolItem, SourcePoolViewData } from "./source-pool-view";

/** How often a card re-reads its Source's first photos and counts. */
const CARD_CADENCE_MS = 30000;

/** A saved Photo source as the snapshot serves it (`media.sources[]`). */
export interface ServedSource {
  source_ref: string;
  check?: CheckState | null;
  /** Its query (`SourceQuery`), absent for a Source not served. */
  query?: unknown;
}

/**
 * A Source's counts and first photos, re-read every {@link CARD_CADENCE_MS} and whenever its
 * served check changes (`checkKey`); null until read.
 */
export function useSourceSummary(sourceRef: string, checkKey = ""): SourcePoolViewData | null {
  const [view, setView] = useState<SourcePoolViewData | null>(null);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      const result = (await sourcePool(sourceRef)) as { ok: boolean; pool?: SourcePoolViewData };
      if (!live) return;
      if (result.ok && result.pool) setView(result.pool);
      timer = setTimeout(() => void tick(), CARD_CADENCE_MS);
    };
    void tick();
    return () => {
      live = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [sourceRef, checkKey]);
  return view;
}

/** "2,301 photos · 14 videos", or "Reading…" until the counts are read. */
export function sourceCountLine(view: SourcePoolViewData | null): string {
  if (view === null) return "Reading…";
  return `${counted(view.in_pool.photos, "photo")} · ${counted(view.in_pool.videos, "video")}`;
}

/** The first four photos of the pool, as a card's mosaic. */
function mosaicOf(items: readonly PoolItem[]) {
  return items.slice(0, 4).map((item) => ({
    key: item.asset_id,
    src: item.thumbnail,
    alt: `${item.kind === "video" ? "Video" : "Photo"} dated ${captureDay(item.taken_at)}`,
  }));
}

export interface SourceCardProps {
  source: ServedSource;
  /** The Source's name, as the console shows it. */
  name: string;
  /** The media read time (`media.read_at`): check times are told against it. */
  now: number | null;
  /** Where the name leads: the Source's editor. */
  href?: string;
  /** Check now's receipt, while it waits for the check it asked for. */
  note?: string | null;
  /** Its overflow actions (the Sources list). */
  actions?: readonly MenuItem[];
  /** It is a choice in a form (the Scene editor's Photos step). */
  choice?: CardChoice;
  /** How its photos fit the Frames, in one line (follow-ups C1). */
  fit?: React.ReactNode;
}

/**
 * SourceCard: a saved Photo source as a MediaCard: its first four photos, its photo and video
 * counts, its check's state (sourceWords.js, the one home of those words) and its actions, or
 * its choice in a form.
 */
export function SourceCard({ source, name, now, href, note = null, actions, choice, fit }: SourceCardProps) {
  const view = useSourceSummary(source.source_ref, JSON.stringify(source.check ?? null));
  const state = checkWords(source.check ?? null, now);
  return (
    <MediaCard
      title={name}
      href={href}
      mosaic={mosaicOf(view?.items ?? [])}
      line={sourceCountLine(view)}
      detail={"query" in source ? queryWords(source.query ?? null) : undefined}
      verdict={{ severity: state.severity, label: state.label, receipt: null }}
      note={note}
      menu={actions ? { label: `More actions for ${name}`, items: actions } : undefined}
      choice={choice}
      footer={fit}
    />
  );
}
