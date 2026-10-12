import { useState } from "react";

import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { SaveBar } from "../patterns/save-bar";
import { SettingRow } from "../patterns/setting-row";
import { dayOfWords, houseParts, timeOfDayWords } from "../timeWords.js";
import { Button } from "../ui/button";
import { Field, NumberInput, TextInput } from "../ui/field";
import { MultiToggle } from "../ui/multi-toggle";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
import { Stack } from "../ui/stack";

/** A start or end on the house clock (central/house_time/model.py `TimeOfDay`). */
export type TimeOfDay =
  | { at: "clock"; time: string }
  | { at: "sunrise" | "sunset"; offset_minutes: number };
export type Weekday = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

/** What the editor saves (central/power/model.py `NightOffDraft`). */
export interface NightOffDraft {
  off: TimeOfDay;
  on: TimeOfDay;
  days: Weekday[];
  frames: { kind: "frames"; frame_ids: string[] } | { kind: "all-frames" };
  darkness: "display-off" | "black-picture";
  hold_lasts: "next-scheduled-change" | "until-resumed";
}

/** GET /v1/operator/power/night-off (central/power/views.py `NightOffView`). */
export interface NightOffView {
  night_off: NightOffDraft & { revision: number };
  next_off: string | null;
  next_on: string | null;
}

const DAYS: readonly { value: Weekday; label: string }[] = [
  { value: "mon", label: "Mon" }, { value: "tue", label: "Tue" }, { value: "wed", label: "Wed" },
  { value: "thu", label: "Thu" }, { value: "fri", label: "Fri" }, { value: "sat", label: "Sat" },
  { value: "sun", label: "Sun" },
];
const AT = [
  { value: "clock", label: "Clock time" }, { value: "sunrise", label: "Sunrise" }, { value: "sunset", label: "Sunset" },
] as const;
const CLOCK = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
// On a power screen "on" and "off" would read as display power: a chosen night or Frame is ticked.
const CHOSEN = ["✓", ""] as const;

export interface TimeOfDayControlProps {
  label: string;
  /** A clock time the help shows as its example, as "07:00". */
  example: string;
  value: TimeOfDay;
  onChange: (next: TimeOfDay) => void;
  /** The house has a location: sunrise and sunset can be chosen. */
  placeSet: boolean;
  houseHref: string;
  /** When the saved sun time next falls, in words ("18:12 today"), from Central; null: a clock
   * time, or not known (a change not saved yet). */
  resolved?: string | null;
}

const SIDES = [{ value: "before", label: "Before" }, { value: "after", label: "After" }] as const;
/** Before the sun: a negative offset, or −0 (Before chosen at 0 minutes, until a number is typed). */
export const isBefore = (minutes: number) => minutes < 0 || Object.is(minutes, -0);

/**
 * A clock time, or sunrise or sunset so many minutes before or after (#80, #81); sun times wait
 * for a Place. The side is chosen, not typed as a sign: a typed "−" was lost on the way.
 */
export function TimeOfDayControl({ label, example, value, onChange, placeSet, houseHref, resolved = null }: TimeOfDayControlProps) {
  const clock = value.at === "clock";
  return (
    <Stack>
      <SegmentedControl label={`${label} at`} value={value.at} options={AT} disabled={!placeSet && clock}
        onChange={(at) => onChange(at === "clock" ? { at, time: "" } : { at, offset_minutes: clock ? 0 : value.offset_minutes })} />
      {!placeSet && clock && (
        <>
          <Note>Sunrise and sunset need the house location.</Note>
          <OwnerLinks links={[{ text: "Set the house location", href: houseHref }]} />
        </>
      )}
      {value.at === "clock" ? (
        <Field label={label} help={`On the house clock, as ${example}.`}
          error={CLOCK.test(value.time) ? undefined : `Enter a time as ${example}.`}>
          <TextInput value={value.time} onChange={(time) => onChange({ at: "clock", time })} />
        </Field>
      ) : (
        <>
          <SegmentedControl label={`${label}: before or after ${value.at}`} options={SIDES}
            value={isBefore(value.offset_minutes) ? "before" : "after"}
            onChange={(side) => onChange({ ...value, offset_minutes: (side === "before" ? -1 : 1) * Math.abs(value.offset_minutes) })} />
          <Field label={`${label}: minutes ${isBefore(value.offset_minutes) ? "before" : "after"} ${value.at}`} help="0 to 180.">
            <NumberInput min={0} max={180} step={5} value={Math.abs(value.offset_minutes)}
              onChange={(text) => {
                const minutes = Math.min(180, Math.abs(Math.round(Number(text) || 0)));
                onChange({ ...value, offset_minutes: isBefore(value.offset_minutes) ? -minutes : minutes });
              }} />
          </Field>
          <Note>{resolved === null ? `Save to see when ${timeOfDayWords(value)} falls.` : `Next: about ${resolved}.`}</Note>
        </>
      )}
    </Stack>
  );
}

/** The draft's problem before it is sent, or null. */
function problemOf(draft: NightOffDraft): string | null {
  if (draft.off.at === "clock" && !CLOCK.test(draft.off.time)) return "Enter when displays turn off, as 23:00.";
  if (draft.on.at === "clock" && !CLOCK.test(draft.on.time)) return "Enter when displays turn on, as 07:00.";
  if (draft.days.length === 0) return "Choose at least one night.";
  return null;
}

/** Central's refusal codes, in words (central/power/ports.py `PowerRefused`). */
const REFUSALS: Record<string, string> = {
  "no-days": "Choose at least one night.",
  "needs-place": "Sunrise and sunset need the house location.",
  "unknown-frame": "A chosen Frame no longer exists; choose again.",
  unreachable: "Central did not answer. Save again.",
};

/** A house time as "Mon 12 Oct 23:00". */
function dayTime(local: string): string {
  const { day, time } = houseParts(local);
  return `${day} ${time}`;
}

/**
 * A saved off or on time in words, a sun time with when it next falls from Central's next time:
 * "sunset −30 min, about 18:12 today" (design language §8 E3); a clock time as it is, "23:00".
 */
function savedWords(time: TimeOfDay, next: string | null, now: string | null): string {
  if (time.at === "clock" || next === null) return timeOfDayWords(time);
  return `${timeOfDayWords(time)}, about ${dayOfWords(next, now)}`;
}

/** When a shown sun time next falls, from Central's next time, only while it is the saved one. */
function resolvedOf(shown: TimeOfDay, saved: TimeOfDay, next: string | null, now: string | null): string | null {
  return next === null || JSON.stringify(shown) !== JSON.stringify(saved) ? null : dayOfWords(next, now);
}

const draftOf = (view: NightOffView): NightOffDraft => {
  const { off, on, days, frames, darkness, hold_lasts: holdLasts } = view.night_off;
  return { off, on, days: [...days], frames, darkness, hold_lasts: holdLasts };
};

export interface NightOffEditorProps {
  view: NightOffView;
  /** Every current Frame, by name. */
  frames: readonly { id: string; name: string }[];
  placeSet: boolean;
  houseHref: string;
  /** Central's read time on the house clock, for "today" and "tomorrow"; null: not known. */
  now?: string | null;
  /** Save from the revision the editor opened at; resolves with Central's answer. */
  onSave: (revision: number, draft: NightOffDraft) => Promise<{ ok: true } | { ok: false; error: string }>;
}

/**
 * The Power lane's Night off rules (roadmap 2a, #80-#83, #90; the editor save model): when
 * displays turn off and on (a clock time, or sunrise or sunset plus or minus minutes), on which
 * nights, on which Frames (each current Frame, or the "All Frames" group, which later Frames
 * join), whether "off" turns displays off or shows black, and how long All off and All on hold.
 * Save sends the revision the editor opened at: a save from another tab since answers "changed",
 * and the editor offers Reload rather than overwrite it.
 */
export function NightOffEditor({ view, frames, placeSet, houseHref, now = null, onSave }: NightOffEditorProps) {
  const [draft, setDraft] = useState<NightOffDraft | null>(null);
  const [openedAt, setOpenedAt] = useState(view.night_off.revision);
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const shown = draft ?? draftOf(view);
  const changed = draft !== null && JSON.stringify(draft) !== JSON.stringify(draftOf(view));
  const edit = (patch: Partial<NightOffDraft>) => {
    if (draft === null) setOpenedAt(view.night_off.revision);
    setDraft({ ...shown, ...patch });
    setSaid(null);
  };
  const reload = () => {
    setDraft(null);
    setStale(false);
    setOpenedAt(view.night_off.revision);
    setSaid("Reloaded the saved Night off.");
  };
  const save = async () => {
    const problem = problemOf(shown);
    if (problem !== null) {
      setSaid(`Not saved: ${problem}`);
      return;
    }
    setSaving(true);
    const answer = await onSave(openedAt, shown);
    setSaving(false);
    if (answer.ok) {
      setDraft(null);
      setSaid("Saved. The displays follow it from its next off or on.");
    } else if (answer.error === "changed") {
      setStale(true);
      setSaid(null);
    } else {
      setSaid(`Not saved: ${REFUSALS[answer.error] ?? `Central refused it (${answer.error}).`}`);
    }
  };
  const chosen = shown.frames.kind === "frames" ? shown.frames.frame_ids : [];
  return (
    <Section title="Night off">
      <Note>
        {`Displays turn off at ${savedWords(view.night_off.off, view.next_off, now)} and on at `
          + `${savedWords(view.night_off.on, view.next_on, now)}`
          + (view.next_off === null || view.night_off.off.at !== "clock" ? "." : `; next off ${dayTime(view.next_off)}.`)}
      </Note>
      {stale && (
        <Stack label="Night off changed">
          <Note live>Night off changed since you opened it, in another tab or window. Reload it to see the saved rules; Save waits until you do.</Note>
          <Button onClick={reload}>Reload Night off</Button>
        </Stack>
      )}
      <TimeOfDayControl label="Turn off" example="23:00" value={shown.off} onChange={(off) => edit({ off })} placeSet={placeSet}
        houseHref={houseHref} resolved={resolvedOf(shown.off, view.night_off.off, view.next_off, now)} />
      <TimeOfDayControl label="Turn on" example="07:00" value={shown.on} onChange={(on) => edit({ on })} placeSet={placeSet}
        houseHref={houseHref} resolved={resolvedOf(shown.on, view.night_off.on, view.next_on, now)} />
      <Note>An on time at or before the off time is the next morning: off 23:00, on 07:00 the next day.</Note>
      <SettingRow kind="house" label="Nights" help="The nights Night off starts on." defaultLabel="Default: every night"
        state="idle" control={<MultiToggle label="Nights" value={shown.days} options={DAYS} required stateWords={CHOSEN}
          onChange={(days) => edit({ days: DAYS.map((day) => day.value).filter((day) => days.includes(day)) })} />} />
      <SettingRow kind="house" label="Which Frames" defaultLabel="Default: every Frame" state="idle"
        help="All Frames: a Frame added later follows Night off too."
        control={<SegmentedControl label="Which Frames" value={shown.frames.kind}
          options={[{ value: "all-frames", label: "All Frames" }, { value: "frames", label: "Chosen Frames" }]}
          onChange={(kind) => edit({ frames: kind === "all-frames" ? { kind }
            : { kind, frame_ids: frames.map((frame) => frame.id).sort() } })} />} />
      {shown.frames.kind === "frames" && (
        <Field label="Frames">
          <MultiToggle label="Frames" value={chosen} stateWords={CHOSEN}
            options={frames.map((frame) => ({ value: frame.id, label: frame.name }))}
            onChange={(frameIds) => edit({ frames: { kind: "frames", frame_ids: [...frameIds].sort() } })} />
        </Field>
      )}
      <SettingRow kind="house" label="Off means" defaultLabel="Default: Turn displays off" state="idle"
        help="Show black keeps each display on and draws black, for a display that wakes slowly."
        control={<SegmentedControl label="Off means" value={shown.darkness}
          options={[{ value: "display-off", label: "Turn displays off" }, { value: "black-picture", label: "Show black" }]}
          onChange={(darkness) => edit({ darkness })} />} />
      <SettingRow kind="house" label="A hold lasts" defaultLabel="Default: until the next scheduled change" state="idle"
        help="How long All off and All on keep the displays as they are."
        control={<SegmentedControl label="A hold lasts" value={shown.hold_lasts}
          options={[{ value: "next-scheduled-change", label: "Until the next scheduled change" },
            { value: "until-resumed", label: "Until I resume" }]}
          onChange={(holdLasts) => edit({ hold_lasts: holdLasts })} />} />
      <SaveBar label="Night off actions" message={said !== null && <Note live>{said}</Note>}>
        <Button variant="primary" disabled={!changed || saving || stale} onClick={() => void save()}>
          {saving ? "Saving…" : "Save Night off"}
        </Button>
        <Button variant="ghost" disabled={!changed || saving} onClick={() => { setDraft(null); setSaid(null); }}>
          Discard changes
        </Button>
      </SaveBar>
    </Section>
  );
}
