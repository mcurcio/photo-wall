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
type HardwareRow = View["hardware"][number];
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

export const DISPLAY_GROUP = "Display (hardware)";
export const PHOTO_WALL_GROUP = "Photo Wall (software)";
const AS_IT_HAS_IT = "As the display has it";
const NOT_CONNECTED = "Applies when the display is connected";
const NO_CHANGE = "100 % changes nothing";

const COPY_WORDS: Record<CopyOutcome, string> = {
  copied: "Copied",
  "no-display": "Not copied: the Pi has not reported a display there yet",
  "same-display": "Not copied: it shows on this same display",
  "session-open": "Not copied: someone is adjusting that display now",
};

/**
 * One display setting's line (design "What the tab says"): as the display has it when nobody set
 * it here, whether the display took the latest write, else the value set in Photo Wall on the
 * display's own count ("46 of 100").
 */
export function hardwareWords(view: View, row: HardwareRow, value: number | null, touched: boolean): string {
  if (view.connected === false) return NOT_CONNECTED;
  if (value === null) return AS_IT_HAS_IT;
  if (!touched && (row.result?.result === "not-taken" || row.result?.result === "no-answer")) {
    return "The display did not take it";
  }
  const maximum = row.offered?.range.maximum;
  return maximum == null ? `Set in Photo Wall · ${value}` : `Set in Photo Wall · ${value} of ${maximum}`;
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

/** Display (hardware): a slider per level the display offers, with "As the display has it"; a list
 * setting as a read-only line (slice S5 adds its picker). */
function DisplaySettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const { draft } = session;
  return (
    <FactGroup title={DISPLAY_GROUP}>
      <Stack>
        {!view.offered_known && <Note>Checking what this display offers…</Note>}
        {view.hardware.map((row) => {
          const touched = row.code in draft.hardware;
          const value = touched ? draft.hardware[row.code]! : row.value;
          const range = row.offered?.range;
          if (range?.kind !== "level" || range.maximum === null) {
            const shown = value ?? row.offered?.current;
            return (
              <FactRow key={row.code} label={row.name} tone="reported">
                {shown == null ? "No answer" : String(shown)}
              </FactRow>
            );
          }
          const set = (next: number) => session.changeHardware(row.code, next);
          return (
            <Stack key={row.code}>
              <Field label={row.name} notes={[hardwareWords(view, row, value, touched)]}>
                <Slider min={0} max={range.maximum} step={1} unit="" value={value ?? row.offered?.current ?? 0}
                  disabled={!ready} onPreview={set} onCommit={set} />
              </Field>
              {value !== null && (
                <Inline>
                  <Button variant="ghost" disabled={!ready} onClick={() => session.changeHardware(row.code, null)}>
                    {`${AS_IT_HAS_IT}: ${row.name.toLowerCase()}`}
                  </Button>
                </Inline>
              )}
            </Stack>
          );
        })}
      </Stack>
    </FactGroup>
  );
}

/** Photo Wall (software): brightness and contrast 0–150 % (100 % changes nothing), colour
 * temperature and gamma; the picture Photo Wall sends, never the display's own brightness. */
function PhotoWallSettings({ view, session, ready }: { view: View; session: PictureSession; ready: boolean }) {
  const { software } = session.draft;
  const percent = (setting: "brightness" | "contrast", label: string, inForce: number) => {
    const value = software[setting] ?? inForce;
    return (
      <Field label={label} notes={[`${value} % · ${NO_CHANGE}`]}>
        <Slider min={0} max={150} step={1} unit="%" value={value} disabled={!ready}
          onPreview={(next) => session.changeSoftware(setting, next)}
          onCommit={(next) => session.changeSoftware(setting, next)} />
      </Field>
    );
  };
  const kelvin = software["colour-temperature"] ?? view.software.colour_temperature_kelvin;
  const gamma = software.gamma ?? view.software.gamma_tenths;
  return (
    <FactGroup title={PHOTO_WALL_GROUP}>
      <Stack>
        {percent("brightness", "Brightness", view.software.brightness_percent)}
        {percent("contrast", "Contrast", view.software.contrast_percent)}
        <Field label="Colour temperature" notes={[`${kelvin} K`]}>
          <Slider min={3000} max={9300} step={100} unit="K" disabled={!ready} value={kelvin}
            onPreview={(next) => session.changeSoftware("colour-temperature", next)}
            onCommit={(next) => session.changeSoftware("colour-temperature", next)} />
        </Field>
        <Field label="Gamma" notes={[(gamma / 10).toFixed(1)]}>
          <Slider min={1.8} max={2.6} step={0.1} unit="" disabled={!ready} value={gamma / 10}
            onPreview={(next) => session.changeSoftware("gamma", Math.round(next * 10))}
            onCommit={(next) => session.changeSoftware("gamma", Math.round(next * 10))} />
        </Field>
      </Stack>
    </FactGroup>
  );
}

/** Copy to other Frames (#119): Photo Wall's four and the display settings set here, never position. */
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
          Copies this display&apos;s kept Photo Wall settings, and the display settings set here where the
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
