import { useState } from "react";

import { levelWords } from "../levelWords.js";
import { copyPicture } from "../pictureApi.js";
import type { HardwareSettingView, PictureCopyResult, SoftwareSetting } from "../pictureApi.js";
import type { usePictureSession } from "../pictureSession.js";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { LivePreviewEditor } from "../patterns/live-preview-editor";
import { ProblemCard } from "../patterns/problem-card";
import { timeOfDay } from "../timeWords.js";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { Field } from "../ui/field";
import { Link } from "../ui/link";
import { Section } from "../ui/section";
import { Select } from "../ui/select";
import { Slider } from "../ui/slider";
import { Inline, Stack } from "../ui/stack";
import { Switch } from "../ui/switch";

/** The Picture tab's live session, as pictureSession.js serves it. */
export type PictureSession = ReturnType<typeof usePictureSession>;
type View = NonNullable<PictureSession["view"]>;
type CopyOutcome = PictureCopyResult["outcome"];

export interface FramePictureProps {
  frameId: string;
  /** The Frame's Pi's page, or null when unbound. */
  piHref: string | null;
  /** The tab's live session (the Frame page holds it, so its LeaveGuard covers it). */
  session: PictureSession;
  /** The other Frames on the wall, for Copy. */
  otherFrames: readonly string[];
}

export const DISPLAY_GROUP = "Display (hardware)";
export const PHOTO_WALL_GROUP = "Photo Wall (software)";
const AS_IT_HAS_IT = "As the display has it";
const NOT_CONNECTED = "Applies when the display is connected";
const NO_CHANGE = "100 % changes nothing";
const ZERO_NO_CHANGE = "0 % changes nothing";
const ACTS_ABOVE_100 = "acts above 100 % brightness";
// The LUT picker's "None" (a choice's own value is its paper profile's digest).
const NO_LUT = "none";
const NO_LUTS = "No LUTs on the list yet";
const LUT_WAITS = "The sliders show at once; the LUT joins when the Pi has it, and Done waits for it";
const CHECKING = "Checking what this display offers…";
const CHECKED_WHEN_CONNECTED = "Photo Wall checks what this display offers when it is connected";
const NOT_OFFERED = "Not offered by this display at its last check";
// The Select's value for "As the display has it" (a choice's own value is its byte, in decimal).
const AS_IT_HAS_IT_OPTION = "as-the-display-has-it";

const COPY_WORDS: Record<CopyOutcome, string> = {
  copied: "Copied",
  "no-display": "Not copied: the Pi has not reported a display there yet",
  "same-display": "Not copied: it shows on this same display",
  "session-open": "Not copied: someone is adjusting that display now",
};

/** A display value in the display's own words: a level as a percent of its range, its own count
 * second ("46 % (117 of 255)", levelWords.js), a choice's name ("6500 K"), else the number it
 * answered. */
function displayValue(row: HardwareSettingView, value: number): string {
  const range = row.offered?.range;
  if (range?.kind === "level" && range.maximum !== null) return levelWords(value, range.maximum);
  return range?.choices.find((choice) => choice.value === value)?.name ?? String(value);
}

/**
 * One display setting's words (design "What the tab says"), `draft` the operator's change in this
 * visit (undefined: none; null: handed back), `offeredKnown` the view's: as the display has it when
 * nobody set it here; while what the display offers is not known (never checked yet), the value set
 * here alone; set here but not offered at the display's last check; whether the display took the
 * latest write or was changed on its own buttons since (Central's judgement,
 * central/displays/hardware.py `changed_on_buttons`; never while the operator's own change is in
 * flight); else the value set in Photo Wall, a level as a percent of its range with the display's
 * own count second ("46 % (117 of 255)"), a list setting by its choice's name ("6500 K"). Three
 * states never share one null: not connected (the tab's own line), not checked yet (`offeredKnown`
 * false) and not offered at the last check.
 */
export function hardwareWords(row: HardwareSettingView, draft: number | null | undefined, offeredKnown = true): string {
  const value = draft === undefined ? row.value : draft;
  if (value === null) return AS_IT_HAS_IT;
  if (row.offered === null) return offeredKnown ? NOT_OFFERED : displayValue(row, value);
  if (draft === undefined && (row.result?.result === "not-taken" || row.result?.result === "no-answer")) {
    return "The display did not take it";
  }
  if (draft === undefined && row.changed_on_buttons) return `Changed on the display's buttons: ${displayValue(row, row.offered.current!)}`;
  return displayValue(row, value);
}

/** Copy's words for one target: its outcome, and the display settings left as they are there,
 * named from `names` (the source's rows: code to name) or by code. */
export function copyWords(result: PictureCopyResult, names: Readonly<Record<string, string>> = {}): string {
  const skipped = result.hardware_skipped.map((code) => names[code] ?? code);
  return skipped.length === 0 ? COPY_WORDS[result.outcome] : `${COPY_WORDS[result.outcome]}; skipped: ${skipped.join(", ")}`;
}

function unavailableWords(session: PictureSession, piHref: string | null) {
  if (session.unavailable === null) return null;
  return (
    <>
      {session.unavailable.words}
      {session.unavailable.code === "no_display" && piHref !== null && (
        <>{" "}<Link href={piHref}>Open the Pi&apos;s software page</Link></>
      )}
    </>
  );
}

/** One display setting: a slider per level, a picker per list of choices (its choices and "As the
 * display has it"), a line with no control for a setting the display answers but lists nothing for,
 * or one it no longer offers; a hand-back ("As the display has it: …") wherever a value is set. */
function HardwareSettingRow({ row, session, ready, offeredKnown }: {
  row: HardwareSettingView; session: PictureSession; ready: boolean; offeredKnown: boolean;
}) {
  const { draft } = session;
  const drafted = row.code in draft.hardware ? draft.hardware[row.code] : undefined;
  const value = drafted === undefined ? row.value : drafted;
  const words = hardwareWords(row, drafted, offeredKnown);
  const set = (next: number | null) => session.changeHardware(row.code, next);
  const range = row.offered?.range;
  const handBack = value !== null && range?.kind !== "choice" && (
    <Inline>
      <Button variant="ghost" disabled={!ready} onClick={() => set(null)}>
        {`${AS_IT_HAS_IT}: ${row.name.toLowerCase()}`}
      </Button>
    </Inline>
  );
  if (range?.kind === "level" && range.maximum !== null) {
    return (
      <Stack>
        <Field label={row.name} notes={[words]}>
          <Slider min={0} max={range.maximum} step={1} unit="" value={value ?? row.offered?.current ?? 0}
            format={(shown) => levelWords(shown, range.maximum!)} disabled={!ready} onPreview={set} onCommit={set} />
        </Field>
        {handBack}
      </Stack>
    );
  }
  if (range?.kind === "choice") {
    const options = [
      { value: AS_IT_HAS_IT_OPTION, label: AS_IT_HAS_IT },
      ...range.choices.map((choice) => ({ value: String(choice.value), label: choice.name })),
    ];
    const chosen = value === null ? AS_IT_HAS_IT_OPTION : String(value);
    return (
      <Field label={row.name} notes={[words]}>
        <Select value={options.some((option) => option.value === chosen) ? chosen : null} options={options}
          disabled={!ready} onChange={(next) => set(next === AS_IT_HAS_IT_OPTION ? null : Number(next))} />
      </Field>
    );
  }
  const current = row.offered?.current;
  return (
    <Stack>
      <FactRow label={row.name} tone="reported">
        {row.offered === null || value !== null ? words : current == null ? "No answer" : displayValue(row, current)}
      </FactRow>
      {handBack}
    </Stack>
  );
}

/** Display (hardware): every setting the display offers, and every one set here, in picture-list
 * order; "Checking what this display offers" while a connected display's list is not known yet,
 * and that it is checked once connected while one never checked is away. */
function DisplaySettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  return (
    <FactGroup title={DISPLAY_GROUP}>
      <Stack>
        {!view.offered_known && <Note>{view.connected === false ? CHECKED_WHEN_CONNECTED : CHECKING}</Note>}
        {view.connected === false && <Note>{NOT_CONNECTED}</Note>}
        {view.hardware.map((row) => (
          <HardwareSettingRow key={row.code} row={row} session={session} ready={ready} offeredKnown={view.offered_known} />
        ))}
      </Stack>
    </FactGroup>
  );
}

/** Whether Soft highlights acts: only where Photo Wall can push a value past white, its brightness
 * or saturation above 100 % (design "Highlight rolloff, defined for SDR"; the Pi skips it otherwise). */
export function softHighlightsAct(brightnessPercent: number, saturationPercent: number): boolean {
  return brightnessPercent > 100 || saturationPercent > 100;
}

/** Black floor's words: tenths of a percent of white light as a percent with one decimal ("2.0 %"). */
export function blackFloorWords(tenths: number): string {
  return `${(tenths / 10).toFixed(1)} %`;
}

/** The LUT picker's choices: None, each LUT on the list, and the one in force if the list (not read
 * yet, or changed since) lacks it. */
export function lutOptions(luts: readonly { profile_sha256: string; name: string }[] | null,
  inForce: { profile_sha256: string; name: string } | null): { value: string; label: string }[] {
  const listed = luts ?? [];
  const extra = inForce !== null && !listed.some((lut) => lut.profile_sha256 === inForce.profile_sha256) ? [inForce] : [];
  return [{ value: NO_LUT, label: "None" },
    ...[...listed, ...extra].map((lut) => ({ value: lut.profile_sha256, label: lut.name }))];
}

/** The LUT (print look R3): None or one on the LUTs list, with its amount 0–100 % only with a LUT.
 * A LUT the Pi has not fetched yet leaves the sliders showing at once; Done waits for it. */
export function LutSettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const lutDraft = session.draft.lut;
  const lut = "lut" in lutDraft ? lutDraft.lut ?? null : view.software.lut?.profile_sha256 ?? null;
  const amount = session.draft.software["lut-amount"] ?? view.software.lut_amount_percent;
  const options = lutOptions(session.luts, view.software.lut);
  const notes = [
    ...(options.length === 1 ? [NO_LUTS] : []),
    ...("lut" in lutDraft && lutDraft.lut != null && session.phase === "waiting" ? [LUT_WAITS] : []),
  ];
  const send = (next: number) => session.changeSoftware("lut-amount", next);
  return (
    <Stack>
      <Field label="LUT" notes={notes}>
        <Select value={lut ?? NO_LUT} options={options} disabled={!ready}
          onChange={(next) => session.changeLut(next === NO_LUT ? null : next)} />
      </Field>
      {lut !== null && (
        <Field label="LUT amount" notes={[`${amount} % · 0 % changes nothing`]}>
          <Slider min={0} max={100} step={1} unit="%" value={amount} disabled={!ready} onPreview={send} onCommit={send} />
        </Field>
      )}
    </Stack>
  );
}

/** Photo Wall (software): brightness and contrast 0–150 % (100 % changes nothing), colour
 * temperature and gamma, then the print look (design R2, R3, R4): saturation 0–150 % (100 % changes
 * nothing), soft highlights 0–100 % (acts above 100 % brightness), the black floor, 0.0–10.0 % of
 * white in 0.1 steps, sent as tenths, and the LUT: None or one on the LUTs list, with its amount
 * 0–100 % shown only with a LUT. A LUT the Pi has not fetched yet leaves the sliders showing at once;
 * Done waits for it. The picture Photo Wall sends, never the display's own brightness. */
export function PhotoWallSettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const { software } = session.draft;
  const send = (setting: SoftwareSetting) => (next: number) => session.changeSoftware(setting, next);
  const percent = (setting: SoftwareSetting, label: string, value: number, max: number, notes: string[]) => (
    <Field label={label} notes={[[`${value} %`, ...notes].join(" · ")]}>
      <Slider min={0} max={max} step={1} unit="%" value={value} disabled={!ready}
        onPreview={send(setting)} onCommit={send(setting)} />
    </Field>
  );
  const brightness = software.brightness ?? view.software.brightness_percent;
  const contrast = software.contrast ?? view.software.contrast_percent;
  const kelvin = software["colour-temperature"] ?? view.software.colour_temperature_kelvin;
  const gamma = software.gamma ?? view.software.gamma_tenths;
  const saturation = software.saturation ?? view.software.saturation_percent;
  const rolloff = software["highlight-rolloff"] ?? view.software.highlight_rolloff_percent;
  const floor = software["black-floor"] ?? view.software.black_floor_tenths;
  const tenths = (next: number) => session.changeSoftware("black-floor", Math.round(next * 10));
  return (
    <FactGroup title={PHOTO_WALL_GROUP}>
      <Stack>
        {percent("brightness", "Brightness", brightness, 150, [NO_CHANGE])}
        {percent("contrast", "Contrast", contrast, 150, [NO_CHANGE])}
        <Field label="Colour temperature" notes={[`${kelvin} K`]}>
          <Slider min={3000} max={9300} step={100} unit="K" disabled={!ready} value={kelvin}
            onPreview={send("colour-temperature")} onCommit={send("colour-temperature")} />
        </Field>
        <Field label="Gamma" notes={[(gamma / 10).toFixed(1)]}>
          <Slider min={1.8} max={2.6} step={0.1} unit="" disabled={!ready} value={gamma / 10}
            onPreview={(next) => session.changeSoftware("gamma", Math.round(next * 10))}
            onCommit={(next) => session.changeSoftware("gamma", Math.round(next * 10))} />
        </Field>
        {percent("saturation", "Saturation", saturation, 150, [NO_CHANGE])}
        {percent("highlight-rolloff", "Soft highlights", rolloff, 100,
          [ZERO_NO_CHANGE, ...(softHighlightsAct(brightness, saturation) ? [] : [ACTS_ABOVE_100])])}
        <Field label="Black floor" notes={[`${blackFloorWords(floor)} of white · 0.0 % changes nothing`]}>
          <Slider min={0} max={10} step={0.1} unit="" disabled={!ready} value={floor / 10}
            format={(shown) => blackFloorWords(Math.round(shown * 10))} onPreview={tenths} onCommit={tenths} />
        </Field>
        <LutSettings view={view} session={session} ready={ready} />
      </Stack>
    </FactGroup>
  );
}

/** Copy to other Frames (#119): all of Photo Wall's values and the display settings set here, never position. */
function CopyToFrames({ frameId, otherFrames, blocked, names }: {
  frameId: string; otherFrames: readonly string[]; blocked: string | null; names: Readonly<Record<string, string>>;
}) {
  const [chosen, setChosen] = useState<readonly string[]>([]);
  const [sending, setSending] = useState(false);
  const [outcomes, setOutcomes] = useState<readonly PictureCopyResult[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  if (otherFrames.length === 0) return null;
  const copy = async () => {
    setSending(true);
    setProblem(null);
    const result = await copyPicture(frameId, [...chosen]) as
      { ok: true; results: PictureCopyResult[] } | { ok: false; error: string };
    setSending(false);
    if (result.ok) {
      setOutcomes(result.results);
      setChosen([]);
    } else {
      setProblem(result.error);
    }
  };
  const count = chosen.length;
  return (
    <Disclosure summary="Copy to other Frames">
      <Stack>
        <Note>
          Copies all of this display&apos;s kept Photo Wall settings, and the display settings set here where the
          other display offers them, to the displays of the Frames you choose. Each Frame&apos;s position
          stays as it is.
        </Note>
        <Stack label="Frames to copy to">
          {otherFrames.map((other) => (
            <Field key={other} label={`Frame ${other}`}>
              <Switch
                checked={chosen.includes(other)}
                onChange={(on) => setChosen(on ? [...chosen, other] : chosen.filter((id) => id !== other))}
              />
            </Field>
          ))}
        </Stack>
        <Inline>
          <Button disabled={count === 0 || sending || blocked !== null} onClick={() => { void copy(); }}>
            {count === 1 ? "Copy to 1 Frame" : `Copy to ${count} Frames`}
          </Button>
        </Inline>
        {blocked !== null && <Note>{blocked}</Note>}
        {outcomes !== null && (
          <FactGroup title="Copy results">
            {outcomes.map((result) => (
              <FactRow key={result.frame_id} label={`Frame ${result.frame_id}`} tone="set">{copyWords(result, names)}</FactRow>
            ))}
          </FactGroup>
        )}
        {problem !== null && (
          <ProblemCard
            scope="central"
            variant="inline"
            live
            verdict={{ severity: "notice", label: "Not copied", receipt: null }}
            what="Photo Wall did not copy the picture settings. Try it again."
            doing={null}
            details={<Note>{`Photo Wall's answer: ${problem}`}</Note>}
          />
        )}
      </Stack>
    </Disclosure>
  );
}

/**
 * The Frame page's Picture tab (roadmap 1c's Live model, design language §8 row B5; decision 0022,
 * the picture split: console design §5 Picture, settings #111-#114, #119): two groups of the display
 * that feeds this Frame, set apart and never coupled. Display (hardware) is the display's own
 * settings that it offers, each untouched until someone sets it here; Photo Wall (software) changes
 * the picture Photo Wall sends and never claims to be the display's brightness. They belong to the
 * display and move with it. Every change is sent as it is made; Done keeps it once the Pi shows it,
 * Revert puts back what was there. Copy to other Frames copies the kept values.
 */
export function FramePicture({ frameId, piHref, session, otherFrames }: FramePictureProps) {
  const { view, phase } = session;
  const reason = unavailableWords(session, piHref);
  const ready = view !== null && phase !== "idle";
  const notes = [
    ...(session.leftAt !== null
      ? [`Your unsaved changes were reverted at ${timeOfDay(session.leftAt)} because you left this Frame's page.`]
      : []),
    ...(session.savedAt !== null && !session.dirty ? [`Saved at ${timeOfDay(session.savedAt)}.`] : []),
  ];
  const { error } = session;

  const controls = view === null ? <Note>Reading the display&apos;s picture…</Note> : (
    <Stack>
      <Note>These settings belong to the display that feeds this Frame and move with it.</Note>
      <DisplaySettings view={view} session={session} ready={ready} />
      <PhotoWallSettings view={view} session={session} ready={ready} />
      <CopyToFrames frameId={frameId} otherFrames={otherFrames}
        names={Object.fromEntries(view.hardware.map((row) => [row.code, row.name]))}
        blocked={session.dirty ? "Press Done or Revert first: Copy copies the kept values." : null} />
    </Stack>
  );

  return (
    <Section title="Picture">
      <LivePreviewEditor
        label="Show on the Display"
        latestRevision={session.latest}
        ack={session.latest !== null && phase === "shown" ? { revision: session.latest, at: null } : null}
        dirty={session.dirty}
        busy={session.busy}
        onDone={() => { void session.done(); }}
        onRevert={() => { void session.revert(); }}
        controls={controls}
        unavailable={reason === null ? undefined : { reason }}
        status={session.waitingFor ?? "Connecting to the Pi…"}
        notes={notes}
        problem={error === null ? undefined : (
          <ProblemCard
            scope="central"
            variant="inline"
            live
            verdict={{ severity: "notice", label: "Not applied", receipt: null }}
            what={error.words}
            doing={null}
            action={{ label: "Revert", onAction: () => { void session.revert(); } }}
            details={<Note>{`Photo Wall's answer: ${error.code}`}</Note>}
          />
        )}
      />
    </Section>
  );
}
