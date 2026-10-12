import { useMemo, useState } from "react";

import { type FitQuestion, PhotoFitBoard, useFitRead } from "../domain/photo-fit-board";
import { type FitSettings, type FitSettingsView, type FitTuning, storedScenes, useFitSettings } from "../domain/photo-fit";
import { EmptyState } from "../patterns/entity-page";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { SaveBar } from "../patterns/save-bar";
import { SettingRow } from "../patterns/setting-row";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { NumberInput } from "../ui/field";
import { Section } from "../ui/section";
import { Slider } from "../ui/slider";
import { Inline, Stack } from "../ui/stack";

type Key = keyof FitTuning;

/** One tuning value as the page asks it: its words, its control, and how its stored value
 * (a fraction, a ratio, inches, pixels or steps) is shown (`scale`, `unit`, `step`). */
interface Row {
  key: Key;
  label: string;
  help: string;
  control: "slider" | "number";
  scale: number;
  unit: string;
  step: number;
  whole?: boolean;
}

/** Shown first: the values a person tunes by taste. */
const SHOWN: readonly Row[] = [
  { key: "crop_limit", label: "Crop limit", control: "slider", scale: 100, unit: "%", step: 1,
    help: "The most of a photo a Frame may cut away to fill itself. Scenes without their own crop limit follow this one." },
  { key: "face_padding", label: "Space kept around a face", control: "slider", scale: 100, unit: "%", step: 1,
    help: "Room kept beside each face, as a share of the face's own width, so a crop never grazes a face." },
  { key: "min_face_share", label: "Smallest face that counts", control: "number", scale: 100, unit: "% of the photo", step: 0.01,
    help: "A face smaller than this (someone far away) does not stop a crop." },
];
/** Under Advanced. */
const ADVANCED: readonly Row[] = [
  { key: "max_lost_interest_density", label: "How much of the subject a crop may lose", control: "slider", scale: 1, unit: "×", step: 0.05,
    help: "A crop may cut away at most this many times as much of the interesting part as of the photo. Lower keeps subjects safer but fits fewer photos." },
  { key: "min_salience_spread", label: "Smallest spread of interest", control: "slider", scale: 100, unit: "%", step: 1,
    help: "Below this, no part of a photo stands out, and any crop of it is fine." },
  { key: "large_frame_inches", label: "Large Frame size for videos", control: "number", scale: 1, unit: "in", step: 1,
    help: "From this diagonal up, a Frame shows a video only when it has at least the rows below." },
  { key: "large_frame_video_short_side", label: "Rows a video needs on a large Frame", control: "number", scale: 1, unit: "px", step: 1, whole: true,
    help: "A video's shorter side, in pixels, on a Frame at least the size above." },
];
/** Under Advanced › Details: precision, not taste. */
const DETAILS: readonly Row[] = [
  { key: "window_steps", label: "Crop positions tried", control: "number", scale: 1, unit: "positions", step: 1, whole: true,
    help: "How finely a crop is slid across a photo to find its best place." },
  { key: "max_still_edge", label: "Longest picture a Player shows", control: "number", scale: 1, unit: "px", step: 1, whole: true,
    help: "No photo's copy is longer than this, and a Frame that would need a longer one shows other photos." },
];
const ROWS = [...SHOWN, ...ADVANCED, ...DETAILS];

/** A stored value as its control shows it ("35" for 0.35 at scale 100). */
function shown(row: Row, value: number): string {
  return String(Math.round(value * row.scale * 10000) / 10000);
}

type Draft = Record<Key, string>;

function draftOf(settings: FitSettings): Draft {
  return Object.fromEntries(ROWS.map((row) => [row.key, shown(row, settings[row.key])])) as Draft;
}

/** What is wrong with a typed value, against Central's range for it, or null. */
function problemOf(row: Row, text: string, view: FitSettingsView): string | null {
  const [low, high] = view.ranges[row.key];
  const range = `from ${shown(row, low)} to ${shown(row, high)} ${row.unit}`;
  const value = Number(text) / row.scale;
  if (text.trim() === "" || !Number.isFinite(value) || value < low || value > high) return `Choose a value ${range}.`;
  if (row.whole && !Number.isInteger(Number(text))) return `Choose a whole number ${range}.`;
  return null;
}

/** The draft as settings, or null while a value is not within its range. */
function settingsOf(draft: Draft, view: FitSettingsView): FitSettings | null {
  if (ROWS.some((row) => problemOf(row, draft[row.key], view) !== null)) return null;
  // Values the page has no control for (the copy steps) are sent back as read.
  return { ...view.settings, ...Object.fromEntries(ROWS.map((row) => [row.key, Number(draft[row.key]) / row.scale])) };
}

function SettingsRow({ row, draft, view, onChange, onDrag }: {
  row: Row; draft: Draft; view: FitSettingsView; onChange: (key: Key, text: string) => void;
  onDrag: (key: Key, text: string | null) => void;
}) {
  const [low, high] = view.ranges[row.key];
  const fallback = view.defaults[row.key];
  const text = draft[row.key];
  const problem = problemOf(row, text, view);
  const control = row.control === "slider" ? (
    <Slider min={Number(shown(row, low))} max={Number(shown(row, high))} step={row.step} unit={row.unit}
      value={Number(text)}
      onPreview={(value) => onDrag(row.key, String(value))}
      onCommit={(value) => {
        onChange(row.key, String(value));
        onDrag(row.key, null);
      }} />
  ) : (
    <Inline>
      <NumberInput min={Number(shown(row, low))} max={Number(shown(row, high))} step={row.step} value={text}
        onChange={(value) => onChange(row.key, value)} />
      <Note>{row.unit}</Note>
    </Inline>
  );
  return (
    <SettingRow kind="house" label={row.label} help={row.help}
      defaultLabel={`Default: ${shown(row, fallback)} ${row.unit}`}
      onReset={Number(text) / row.scale === fallback ? undefined : () => onChange(row.key, shown(row, fallback))}
      state="idle" error={problem ?? undefined} control={control} />
  );
}

/**
 * Settings › Photo fit (`#/photo-fit`; follow-ups C2, design-language T8; owner, 2026-10-11:
 * "Agreed that these should be configurable, and not hardcoded."): every tuning value of the
 * photo fit rule, house-wide, each a SettingRow with Central's range, its default and Reset.
 * Shown: the crop limit (the one Scenes without their own follow), the space kept around a
 * face and the smallest face that counts; under Advanced the rest, the crop positions tried
 * under its Details. Beside them the Fit board of every Source a Scene uses, judged with the
 * values as typed (a dragged slider by shape), so a change shows its effect before Save. Save
 * sends every value; each Frame uses it from its next photo, and the photos showing now finish their
 * turn.
 */
export function PhotoFitSettingsPage({ snapshot }: { snapshot: object | null }) {
  const { read, save, reload } = useFitSettings();
  const view = read.status === "read" ? read.view : null;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [dragged, setDragged] = useState<{ key: Key; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const current = draft ?? (view === null ? null : draftOf(view.settings));
  const judged = current === null || view === null ? null
    : settingsOf(dragged === null ? current : { ...current, [dragged.key]: dragged.text }, view);
  const sources = useMemo(
    () => [...new Set(storedScenes(snapshot).flatMap((scene) => scene.sourceRefs))].sort(),
    [snapshot],
  );
  const question = useMemo((): FitQuestion | null => (sources.length === 0 || judged === null ? null : {
    selection: { sources: sources.slice(0, 32) }, frames: null, settings: judged, crop_limit: null,
  }), [sources, judged]);
  const fit = useFitRead(question, { live: dragged !== null });

  if (read.status === "loading") return <Note>Loading…</Note>;
  if (read.status === "failed" || view === null || current === null) {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the photo fit settings." doing={null}
        action={{ label: "Try again", onAction: reload }} />
    );
  }
  const changed = JSON.stringify(current) !== JSON.stringify(draftOf(view.settings));
  const change = (key: Key, text: string) => {
    setDraft({ ...current, [key]: text });
    setSaid(null);
  };
  const drag = (key: Key, text: string | null) => setDragged(text === null ? null : { key, text });
  const rows = (list: readonly Row[]) => list.map((row) => (
    <SettingsRow key={row.key} row={row} draft={current} view={view} onChange={change} onDrag={drag} />
  ));
  const onSave = async () => {
    const settings = settingsOf(current, view);
    if (settings === null) {
      setSaid("A value is outside its range: see its row.");
      return;
    }
    setSaving(true);
    const refusal = await save(settings);
    setSaving(false);
    if (refusal === null) {
      setDraft(null);
      setSaid("Saved. Each Frame uses it from its next photo; the photos showing now finish their turn.");
    } else {
      setSaid(`Not saved: ${refusal === "unreachable" ? "Central did not answer." : `Central refused it (${refusal}).`}`);
    }
  };
  return (
    <Stack>
      <Note>How Photo Wall decides whether a photo fills a Frame. They apply to every Frame and every Scene.</Note>
      <Section title="Photo fit">
        {rows(SHOWN)}
        <Disclosure summary="Advanced">
          {rows(ADVANCED)}
          <Disclosure summary="Details">{rows(DETAILS)}</Disclosure>
        </Disclosure>
      </Section>
      <Section title="What fits with these values">
        {sources.length === 0
          ? <EmptyState>No Scene shows photos from a Source yet, so there is nothing to judge.</EmptyState>
          : judged === null
            ? <Note>Every value must be within its range before its effect can be shown.</Note>
            : (
              <PhotoFitBoard read={fit} size="compact" title="How the photos fit with these values"
                judged={{ at: "settings" }} />
            )}
      </Section>
      <SaveBar label="Photo fit actions" message={said !== null && <Note live>{said}</Note>}>
        <Button variant="primary" disabled={!changed || saving} onClick={() => void onSave()}>
          {saving ? "Saving…" : "Save photo fit"}
        </Button>
        <Button variant="ghost" disabled={!changed || saving} onClick={() => {
          setDraft(null);
          setSaid(null);
        }}>Discard changes</Button>
        <Note>Each Frame uses it from its next photo; the photos showing now finish their turn.</Note>
      </SaveBar>
    </Stack>
  );
}
