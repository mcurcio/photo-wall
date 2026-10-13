import { useState } from "react";

import { formatAge } from "../health.js";
import { levelWords } from "../levelWords.js";
import { copyPicture, paperTargetUrl } from "../pictureApi.js";
import type {
  HardwareSettingView, PaperSize, PictureCopyResult, SignalRange, SignalView, SoftwareSetting, TestPattern, TestPatternView,
} from "../pictureApi.js";
import type { usePictureSession } from "../pictureSession.js";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { LivePreviewEditor } from "../patterns/live-preview-editor";
import { ProblemCard } from "../patterns/problem-card";
import { formatRoute } from "../routes.js";
import { timeOfDay } from "../timeWords.js";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { Field } from "../ui/field";
import { Link } from "../ui/link";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
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
// Where Soft highlights can act: the Pi's rule (player/geometry.py `rolloff_skipped`) is brightness
// above 100 %, so the row states the condition and never decides it.
const ACTS_ABOVE_100 = "acts only above 100 % brightness";
const BLACK_FLOOR_HELP = "How light black is, as a share of white light";
const ZERO_TENTHS_NO_CHANGE = "0.0 % changes nothing";
// The LUT picker's "None" (a choice's own value is its paper profile's digest).
const NO_LUT = "none";
const LUT_HELP = "Makes photos look printed on one paper";
const NO_LUTS = "No LUTs on the list yet";
const ADD_A_LUT = "Add a paper profile in Settings › LUTs";
const LUT_WAITS = "The sliders show at once; the LUT joins when the Pi has it, and Done waits for it";
const CHECKING = "Checking what this display offers…";
const CHECKED_WHEN_CONNECTED = "Photo Wall checks what this display offers when it is connected";
const NOT_OFFERED = "Not offered by this display at its last check";
// The Select's value for "As the display has it" (a choice's own value is its byte, in decimal).
const AS_IT_HAS_IT_OPTION = "as-the-display-has-it";

export const TEST_PATTERN_GROUP = "Test pattern";
export const SIGNAL_GROUP = "Signal range";
// The Select's value for Off (the wire's is null, never "off").
const OFF = "off";
/** Each test pattern's name: the one home, the Picture History's rows included (picture-history.tsx). */
export const PATTERN_NAMES: Record<TestPattern, string> = {
  white: "White",
  "grey-ramp": "Grey ramp",
  "black-level": "Black level",
  "target-pre-software": "Target: pre-software",
  "target-post-software": "Target: post-software",
};
// Six choices: a Select (design language §4: a SegmentedControl holds two to five).
const PATTERN_OPTIONS: readonly { value: TestPattern | typeof OFF; label: string }[] = [
  { value: OFF, label: "Off" },
  ...(Object.keys(PATTERN_NAMES) as TestPattern[]).map((value) => ({ value, label: PATTERN_NAMES[value] })),
];
// The two screen targets (calibration targets, decision 0026), either in any order (owner,
// 2026-10-13: "thats the users choice -- not something that the software enforces"): the tab never
// orders or gates them. The size the Frame's opening needs is printed on the sheet, never said here.
const TARGETS: ReadonlySet<TestPattern> = new Set<TestPattern>(["target-pre-software", "target-post-software"]);
const PAPER_UNDER_GLASS = "Lay the printed sheet uncut in the dark box under the Frame's glass, a black card behind "
  + "it, and photograph both: ProRAW photos are read for you. The sheet says how large the Frame's opening must be.";
const PAPER_SIZES: readonly { paper: PaperSize; label: string }[] = [
  { paper: "letter", label: "Letter" }, { paper: "a4", label: "A4" },
];
// What to do with each pattern: the one home of these words, the Black level's reading included
// (display tuning, "Reading the Black level"); the console design and the runbook describe and
// link here, never quote.
const PATTERN_HOW: Record<TestPattern, string> = {
  white: "Tape a sheet of white paper beside the Frame and match its white.",
  "target-pre-software": `Judges the display's own settings: the target skips every Photo Wall step. ${PAPER_UNDER_GLASS}`,
  "target-post-software": `Judges Photo Wall's settings: the target goes through them. ${PAPER_UNDER_GLASS}`,
  "grey-ramp": "Every step should be told apart: the darkest from black, the lightest from white.",
  "black-level": "Switch Signal range between Full and Limited. Black darker on Full with every step visible: "
    + "keep Full. Steps vanish into black on Full: choose Limited. No change: the display follows the Pi, the glow "
    + "is its backlight; keep Full, it keeps all 256 steps.",
};
const RANGE_NAMES: Record<SignalRange, string> = { auto: "Auto", full: "Full", limited: "Limited" };
const RANGE_OPTIONS = (["auto", "full", "limited"] as const).map((value) => ({ value, label: RANGE_NAMES[value] }));
const AUTO_CHOOSES = "The Pi chooses: Limited for TV modes.";
// One Weston drives every HDMI port of a Pi, so a change restarts the picture on all of them.
const RANGE_RESTARTS = "Changing it restarts the picture on every display of this Pi: no photos until it is back.";
const WAITING_FOR_THE_PI = "Waiting for the Pi";
const RANGE_WAITING = `${WAITING_FOR_THE_PI}: a change restarts the picture on every display of this Pi.`;
const NO_RANGE_SETTING = "This display's connection has no signal range setting.";
const COPY_NO_RANGE = "Copy never copies the signal range: it follows each display.";

/** A test pattern's state in words; `seconds` how long the Pi keeps one up (the view's
 * `test_pattern_seconds`, never a literal here). */
export function testPatternWords(view: TestPatternView, seconds: number): string {
  // The Player draws a pattern inside a Frame it has drawn (0024, Known limit): after a restart it
  // waits for the Frame's content.
  if (view.state === "waiting") return `${WAITING_FOR_THE_PI}: it shows once the Player draws this Frame.`;
  if (view.state === "shown") return "The Pi reports it shown on every Frame of this display.";
  return `Taken off after ${formatAge(seconds)}. Choose it again to show it.`;
}

/** How a test pattern goes off, in words the operator can act on now: a pattern alone changes
 * nothing Done keeps, so Done and Revert are not named (they stay disabled until something else
 * changes, and then take it off too). */
export function testPatternEnds(seconds: number): string {
  return `Choose Off or leave this page; the Pi takes it off by itself after ${formatAge(seconds)}.`;
}

/**
 * The signal range's line: `range` the one wanted (the operator's choice in this visit, else the
 * view's), `signal` the Pi's answer. While the Pi has not answered a range other than the one its
 * connector holds: waiting for the Pi (a restart is expected, never claimed); then what it sends,
 * that it could not change it (and what the display still gets), or that the connection has no
 * such setting. Auto names what the Pi chooses. No answer yet with Auto wanted: the Pi's choice, as
 * at every start.
 */
export function signalWords(range: SignalRange, signal: SignalView | null): string {
  const sends = (inForce: SignalRange) => (inForce === "auto" ? AUTO_CHOOSES : `The Pi sends ${RANGE_NAMES[inForce]}.`);
  if (signal === null) return range === "auto" ? AUTO_CHOOSES : RANGE_WAITING;
  if (!signal.answered) return signal.in_force === range && signal.result === "applied" ? sends(range) : RANGE_WAITING;
  if (signal.result === "unsupported") return NO_RANGE_SETTING;
  if (signal.result === "failed") {
    const gets = signal.in_force === "auto" ? "what the Pi chooses" : RANGE_NAMES[signal.in_force];
    return `The Pi could not change it: the display still gets ${gets}.`;
  }
  return signal.in_force === range ? sends(range) : RANGE_WAITING;
}

/** The paper target's print links, Letter and A4 (pictureApi.js `paperTargetUrl`); Central answers
 * each as a download. */
function PrintPaperTarget() {
  return (
    <Note>
      {"Print the paper target: "}
      {PAPER_SIZES.map(({ paper, label }, index) => (
        <span key={paper}>{index > 0 && " · "}<Link href={paperTargetUrl(paper)}>{label}</Link></span>
      ))}
    </Note>
  );
}

/** Test pattern (#101): Off, White, Grey ramp, Black level, or a screen target (pre-software or
 * post-software, either in any order) on every Frame of this display, live while the tab is open and
 * never kept; what to do with the chosen one, the paper target's print links with a target, and its
 * state on the Pi. A pattern the Pi took off at its bound shows Off with the words to choose it again. */
export function TestPatternSettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const drafted = session.draft.pattern;
  const asked = view.test_pattern;
  const ended = asked?.state === "ended";
  const pattern = ended ? null : "pattern" in drafted ? drafted.pattern ?? null : asked?.pattern ?? null;
  const seconds = view.test_pattern_seconds;
  return (
    <FactGroup title={TEST_PATTERN_GROUP}>
      <Stack>
        <Field label="Show" help={testPatternEnds(seconds)}>
          <Select value={pattern ?? OFF} options={PATTERN_OPTIONS} disabled={!ready} width="fit"
            onChange={(next) => session.changeTestPattern(next === OFF ? null : next)} />
        </Field>
        {pattern !== null && <Note>{PATTERN_HOW[pattern]}</Note>}
        {pattern !== null && TARGETS.has(pattern) && <PrintPaperTarget />}
        {asked !== null && (
          <FactRow tone={asked.state === "waiting" ? "claimed" : "reported"}>{testPatternWords(asked, seconds)}</FactRow>
        )}
      </Stack>
    </FactGroup>
  );
}

/** Signal range: Auto, Full or Limited, the display's, kept by Done and put back by Revert; never
 * copied. Its line says what the Pi made of it (`signalWords`). */
export function SignalSettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const range = session.draft.signal.range ?? view.signal_range;
  const words = signalWords(range, view.signal);
  return (
    <FactGroup title={SIGNAL_GROUP}>
      <Stack>
        <Field label="Range" help={RANGE_RESTARTS}>
          <SegmentedControl label="Range" value={range} options={RANGE_OPTIONS} disabled={!ready}
            onChange={session.changeSignalRange} />
        </Field>
        <FactRow tone={words === RANGE_WAITING ? "claimed" : "reported"}>{words}</FactRow>
      </Stack>
    </FactGroup>
  );
}

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
      <Field label="LUT" help={LUT_HELP} notes={notes}>
        <Select value={lut ?? NO_LUT} options={options} disabled={!ready} width="fit"
          onChange={(next) => session.changeLut(next === NO_LUT ? null : next)} />
      </Field>
      {options.length === 1 && <OwnerLinks links={[{ text: ADD_A_LUT, href: formatRoute({ section: "luts" }) }]} />}
      {lut !== null && (
        <Field label="LUT amount" notes={[ZERO_NO_CHANGE]}>
          <Slider min={0} max={100} step={1} unit="%" value={amount} disabled={!ready} onPreview={send} onCommit={send} />
        </Field>
      )}
    </Stack>
  );
}

/** Photo Wall (software): brightness and contrast 0–150 % (100 % changes nothing), colour
 * temperature and gamma, then the print look (design R2, R3, R4): saturation 0–150 % (100 % changes
 * nothing), soft highlights 0–100 % (acts only above 100 % brightness, the Pi's rule, stated and
 * never decided here), the black floor, 0.0–10.0 % of white in 0.1 steps, sent as tenths, and the
 * LUT: None or one on the LUTs list, with its amount 0–100 % shown only with a LUT. A LUT the Pi has
 * not fetched yet leaves the sliders showing at once; Done waits for it. Each slider shows its own
 * value; the notes say only what changes nothing and where a setting acts. The picture Photo Wall
 * sends, never the display's own brightness. */
export function PhotoWallSettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const { software } = session.draft;
  const send = (setting: SoftwareSetting) => (next: number) => session.changeSoftware(setting, next);
  const percent = (setting: SoftwareSetting, label: string, value: number, max: number, notes: string[]) => (
    <Field label={label} notes={notes}>
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
        <Field label="Colour temperature">
          <Slider min={3000} max={9300} step={100} unit="K" disabled={!ready} value={kelvin}
            onPreview={send("colour-temperature")} onCommit={send("colour-temperature")} />
        </Field>
        <Field label="Gamma">
          <Slider min={1.8} max={2.6} step={0.1} unit="" disabled={!ready} value={gamma / 10}
            format={(shown) => shown.toFixed(1)}
            onPreview={(next) => session.changeSoftware("gamma", Math.round(next * 10))}
            onCommit={(next) => session.changeSoftware("gamma", Math.round(next * 10))} />
        </Field>
        {percent("saturation", "Saturation", saturation, 150, [NO_CHANGE])}
        {percent("highlight-rolloff", "Soft highlights", rolloff, 100, [ZERO_NO_CHANGE, ACTS_ABOVE_100])}
        <Field label="Black floor" help={BLACK_FLOOR_HELP} notes={[ZERO_TENTHS_NO_CHANGE]}>
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
        <Note>{COPY_NO_RANGE}</Note>
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
 * the picture split: console design §5 Picture, settings #111-#114, #119; display tuning: #101): a
 * test pattern to tune against and the signal range first (you tune by looking at a pattern, and
 * read the Black level by switching the range), then two groups of the display that feeds this
 * Frame, set apart and never coupled. Display (hardware) is the display's own
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
      <TestPatternSettings view={view} session={session} ready={ready} />
      <SignalSettings view={view} session={session} ready={ready} />
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
