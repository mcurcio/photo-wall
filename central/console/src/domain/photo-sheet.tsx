import { useEffect, useState } from "react";

import { photoFacts } from "../libraryApi.js";
import { type CropView, CropThumb } from "../patterns/crop-thumb";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { countText } from "../timeWords.js";
import { Field } from "../ui/field";
import { Select } from "../ui/select";
import { Sheet } from "../ui/sheet";
import { Stack } from "../ui/stack";
import { type FitRefusal, type FitSample, type FrameFit, ratioWords, reasonWords } from "./photo-fit";

/** One of Immich's copies of a photo (central/photo_library/views.py `CopyView`). */
export interface CopyView {
  name: "thumbnail" | "preview" | "fullsize" | "original";
  in_immich: boolean | null;
  measured: { width: number; height: number; media_type: string } | null;
}

/** One pool photo's facts (central/photo_library/views.py `PhotoFacts`). */
export interface PhotoFacts {
  asset_id: string;
  kind: "image" | "video";
  taken_at: number;
  width: number;
  height: number;
  edited: boolean;
  original: { media_type: string | null; width: number | null; height: number | null;
    orientation: number | null; size_bytes: number | null } | null;
  copies: CopyView[];
  prepared_from: CopyView["name"] | null;
}

/** Immich's copies in the photo sheet's words. */
export const COPY_WORDS: Readonly<Record<CopyView["name"], string>> = {
  thumbnail: "Thumbnail",
  preview: "Preview",
  fullsize: "Full-size copy",
  original: "Original",
};

/** A file's bytes in words: "4.2 MB". */
export function bytesWords(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** A file type in words: "image/heic" is "HEIC". */
function typeWords(mediaType: string | null): string | null {
  return mediaType === null ? null : (mediaType.split("/")[1] ?? mediaType).toUpperCase();
}

/** One copy in words: its size as measured, or "size known after first use"; never an assumed
 * size (the original's is Immich's own report, from the photo's facts). */
export function copyWords(copy: CopyView, facts: PhotoFacts): string {
  if (copy.in_immich === false) return "Immich keeps none";
  if (copy.name === "original") {
    const original = facts.original;
    if (original === null || original.width === null || original.height === null) return "size not reported by Immich yet";
    const parts = [`${countText(original.width)} × ${countText(original.height)}`, typeWords(original.media_type),
      original.size_bytes === null ? null : bytesWords(original.size_bytes)];
    return parts.filter((part) => part !== null).join(" · ");
  }
  if (copy.measured === null) return copy.in_immich === null ? "size known after first use (Immich may keep none)" : "size known after first use";
  return `${countText(copy.measured.width)} × ${countText(copy.measured.height)} · ${typeWords(copy.measured.media_type)}`;
}

/** What one Frame's answer says of this photo: it fits (with its window), it is left off (and
 * why), or it is not among that Frame's samples. */
function verdictOn(frame: FrameFit, assetId: string): { verdict: string; sample: FitSample; refused: boolean } | null {
  const fits = [frame.lead, ...frame.fitting].find((sample) => sample?.asset_id === assetId);
  if (fits) return { verdict: "fits", sample: fits, refused: false };
  for (const count of [...frame.refused_by_shape, ...frame.refused_by_content]) {
    const found = count.samples.find((sample) => sample.asset_id === assetId);
    if (found) return { verdict: reasonWords(count.reason as FitRefusal), sample: found, refused: true };
  }
  return null;
}

export interface PhotoSheetProps {
  open: boolean;
  onClose: () => void;
  assetId: string | null;
  /** The thumbnail it was opened from. */
  sample: CropView | null;
  /** The Frames of the answer it was opened from. */
  frames: readonly FrameFit[];
  /** The Frame it was opened on. */
  focus: string | null;
  name?: (frameId: string) => string;
}

type Facts = { status: "loading" } | { status: "read"; facts: PhotoFacts } | { status: "failed"; error: string };

/**
 * The photo sheet (follow-ups C2; Immich's info panel is the model): the photo with each Frame's
 * window, one Frame at a time, and that Frame's verdict in the fit rule's words; the original's
 * type, size and bytes; Immich's copies in order, each with its size as Photo Wall measured it
 * ("size known after first use" before then, never an assumed size); and the copy the wall's
 * prepared one was made from. No "Open in Immich": it would put Immich's id in the page (R22).
 */
export function PhotoSheet({ open, onClose, assetId, sample, frames, focus, name = (id) => id }: PhotoSheetProps) {
  const [facts, setFacts] = useState<Facts>({ status: "loading" });
  const [shown, setShown] = useState<string | null>(focus);
  useEffect(() => setShown(focus), [focus]);
  useEffect(() => {
    if (!open || assetId === null) return undefined;
    let live = true;
    setFacts({ status: "loading" });
    void photoFacts(assetId).then((answer) => {
      if (!live) return;
      const result = answer as { ok: true; facts: PhotoFacts } | { ok: false; error: string };
      setFacts(result.ok ? { status: "read", facts: result.facts } : { status: "failed", error: result.error });
    });
    return () => {
      live = false;
    };
  }, [open, assetId]);

  const known = assetId === null ? [] : frames.map((frame) => ({ frame, found: verdictOn(frame, assetId) }))
    .filter((entry) => entry.found !== null);
  const current = known.find((entry) => entry.frame.frame_id === shown) ?? known[0] ?? null;
  const title = sample === null ? "Photo" : sample.alt;
  const drawn: CropView | null = current === null || sample === null ? sample : {
    ...sample, aspect: current.frame.aspect, offset: current.found!.sample.crop.offset,
    verdict: current.found!.verdict, refused: current.found!.refused,
  };
  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }} title={title}>
      {open && sample !== null && drawn !== null ? (
        <Stack>
          <CropThumb sample={drawn} height={200} />
          {known.length > 1 && (
            <Field label="Frame">
              <Select value={current?.frame.frame_id ?? null}
                options={known.map(({ frame }) => ({ value: frame.frame_id, label: name(frame.frame_id) }))}
                onChange={setShown} />
            </Field>
          )}
          <FactGroup title="On the Frames">
            {known.map(({ frame, found }) => (
              <FactRow key={frame.frame_id} label={`${name(frame.frame_id)} (${ratioWords(frame.aspect)})`} tone="derived">
                {found!.verdict}
              </FactRow>
            ))}
            {known.length < frames.length && (
              <Note>Its fit on the other Frames shows when it is among their photos above.</Note>
            )}
          </FactGroup>
          {facts.status === "loading" && <Note>Reading its copies…</Note>}
          {facts.status === "failed" && (
            <Note>{facts.error === "photo_not_found"
              ? "This photo is no longer in a Source's photos."
              : "Photo Wall could not read its copies just now."}</Note>
          )}
          {facts.status === "read" && (
            <>
              <FactGroup title="The photo">
                <FactRow label="Shown as" tone="reported">
                  {`${countText(facts.facts.width)} × ${countText(facts.facts.height)}${facts.facts.edited ? ", edited in Immich" : ""}`}
                </FactRow>
              </FactGroup>
              <FactGroup title="Copies in Immich">
                {facts.facts.copies.map((copy) => (
                  <FactRow key={copy.name} label={COPY_WORDS[copy.name]} tone="reported">{copyWords(copy, facts.facts)}</FactRow>
                ))}
              </FactGroup>
              <FactRow label="Prepared from" tone="reported">
                {facts.facts.prepared_from === null ? "not prepared yet" : COPY_WORDS[facts.facts.prepared_from].toLowerCase()}
              </FactRow>
            </>
          )}
        </Stack>
      ) : null}
    </Sheet>
  );
}
