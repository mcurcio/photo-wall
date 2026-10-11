import { useState } from "react";

import { copyPicture } from "../pictureApi.js";
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
import { Slider } from "../ui/slider";
import { Inline, Stack } from "../ui/stack";
import { Switch } from "../ui/switch";

/** The Picture tab's live session, as pictureSession.js serves it. */
export type PictureSession = ReturnType<typeof usePictureSession>;
type View = NonNullable<PictureSession["view"]>;
type ActsOn = "display" | "picture-adjustment";
type Placeable = "brightness" | "contrast";
type CopyOutcome = "copied" | "no-display" | "same-display" | "session-open";

export interface FramePictureProps {
  frameId: string;
  /** The Frame's Pi's page, or null when unbound. */
  piHref: string | null;
  /** The tab's live session (the Frame page holds it, so its LeaveGuard covers it). */
  session: PictureSession;
  /** The other Frames on the wall, for Copy. */
  otherFrames: readonly string[];
}

const PLACEABLE: readonly { setting: Placeable; label: string; noun: string }[] = [
  { setting: "brightness", label: "Brightness", noun: "brightness" },
  { setting: "contrast", label: "Contrast", noun: "contrast" },
];
const UNSET_PERCENT = 50;
const ON_DISPLAY = "On the display";
const ADJUSTMENT = "Photo Wall picture adjustment";
const AS_IT_HAS_IT = "As the display has it";
const NOT_CONNECTED = "Applies when the display is connected";

const COPY_WORDS: Record<CopyOutcome, string> = {
  copied: "Copied",
  "no-display": "Not copied: the Pi has not reported a display there yet",
  "same-display": "Not copied: it shows on this same display",
  "session-open": "Not copied: someone is adjusting that display now",
};

/**
 * Where brightness or contrast acts, in the tab's words (design "What the tab and the base page
 * say"): on the display or as Photo Wall picture adjustment, with the value in force; whether the
 * display took it; a value changed on the display's own buttons (only while the draft leaves the
 * setting alone, so the Pi's read of an older value is never read as a button press).
 */
export function whereItActs(view: View, setting: Placeable, actsOn: ActsOn | null, value: number | null,
  touched: boolean): string {
  const row = view[setting];
  if (actsOn === null || view.connected === false) return NOT_CONNECTED;
  if (value === null) return AS_IT_HAS_IT;
  if (actsOn === "picture-adjustment") return `${ADJUSTMENT} · ${value} %`;
  if (row.result === "not-taken" || row.result === "no-answer") return "The display did not take it";
  if (!touched && row.result === "taken" && row.read !== null && row.read !== value) {
    return `Changed on the display's buttons: ${row.read} %`;
  }
  return `${ON_DISPLAY} · ${value} %`;
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

/** Copy to other Frames (#119): the four values this display keeps, never where they act or position. */
function CopyToFrames({ frameId, otherFrames, blocked }: { frameId: string; otherFrames: readonly string[]; blocked: string | null }) {
  const [chosen, setChosen] = useState<readonly string[]>([]);
  const [sending, setSending] = useState(false);
  const [outcomes, setOutcomes] = useState<readonly { frame_id: string; outcome: CopyOutcome }[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  if (otherFrames.length === 0) return null;
  const copy = async () => {
    setSending(true);
    setProblem(null);
    const result = await copyPicture(frameId, [...chosen]) as
      { ok: true; results: { frame_id: string; outcome: CopyOutcome }[] } | { ok: false; error: string };
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
          Copies this display&apos;s four kept values to the displays of the Frames you choose. Where each
          setting acts and each Frame&apos;s position stay as they are. A percent may look different on
          another display.
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
            {outcomes.map(({ frame_id: other, outcome }) => (
              <FactRow key={other} label={`Frame ${other}`} tone="set">{COPY_WORDS[outcome]}</FactRow>
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
 * The Frame page's Picture tab (roadmap 1c, slice K1; design language §8 row B5, the Live model;
 * console design §5 Picture, settings #111-#114, #119): brightness, contrast, colour temperature
 * and gamma of the display that feeds this Frame. They belong to the display and move with it.
 * Each slider says where it acts, live: on the display (its own setting, over the HDMI cable) or
 * as Photo Wall picture adjustment, which changes the picture Photo Wall sends and never claims to
 * be the display's brightness. Brightness and contrast can be moved to picture adjustment, and back
 * onto the display when the display answered. Every change is sent as it is made; Done keeps it
 * once the Pi shows it, Revert puts back what was there. Copy to other Frames copies the values.
 */
export function FramePicture({ frameId, piHref, session, otherFrames }: FramePictureProps) {
  const { view, draft, phase } = session;
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
      {PLACEABLE.map(({ setting, label, noun }) => {
        const touched = setting in draft.values;
        const value = touched ? draft.values[setting]! : view[setting].value;
        const actsOn = draft.places[setting] ?? view[setting].acts_on;
        const set = (next: number) => session.change(setting, next);
        return (
          <Stack key={setting}>
            <Field label={label} notes={[whereItActs(view, setting, actsOn, value, touched)]}>
              <Slider min={0} max={100} step={1} unit="%" value={value ?? view[setting].read ?? UNSET_PERCENT}
                disabled={!ready || actsOn === null} onPreview={set} onCommit={set} />
            </Field>
            {actsOn === "display" && (
              <Inline>
                <Button variant="ghost" disabled={!ready} onClick={() => session.place(setting, "picture-adjustment")}>
                  {`Use picture adjustment for ${noun}`}
                </Button>
              </Inline>
            )}
            {actsOn === "picture-adjustment" && view[setting].answered === true && (
              <Inline>
                <Button variant="ghost" disabled={!ready} onClick={() => session.place(setting, "display")}>
                  {`Use the display's own ${noun}`}
                </Button>
              </Inline>
            )}
          </Stack>
        );
      })}
      <Field label="Colour temperature"
        notes={[`${ADJUSTMENT} · ${draft.values["colour-temperature"] ?? view.colour_temperature_kelvin} K`]}>
        <Slider min={3000} max={9300} step={100} unit="K" disabled={!ready}
          value={draft.values["colour-temperature"] ?? view.colour_temperature_kelvin}
          onPreview={(next) => session.change("colour-temperature", next)}
          onCommit={(next) => session.change("colour-temperature", next)} />
      </Field>
      <Field label="Gamma" notes={[`${ADJUSTMENT} · ${((draft.values.gamma ?? view.gamma_tenths) / 10).toFixed(1)}`]}>
        <Slider min={1.8} max={2.6} step={0.1} unit="" disabled={!ready}
          value={(draft.values.gamma ?? view.gamma_tenths) / 10}
          onPreview={(next) => session.change("gamma", Math.round(next * 10))}
          onCommit={(next) => session.change("gamma", Math.round(next * 10))} />
      </Field>
      <CopyToFrames frameId={frameId} otherFrames={otherFrames}
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
