import { useCallback, useEffect, useState } from "react";

import { readFrameDisplay } from "../displayApi.js";
import { frameProfileProblem, updateFrameProfile } from "../framesApi.js";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { usePolledRead } from "../polledRead.js";
import { orientationCoherent } from "../projection.js";
import { clockTime } from "../timeWords.js";
import { Button } from "../ui/button";
import { Section } from "../ui/section";
import { Stack } from "../ui/stack";
import { useMutate } from "../useMutate.js";
import { useRoute } from "../useRoute.js";
import { useSnapshot } from "../useSnapshot.js";
import type { Readiness } from "./frame-profile";

/** A mode a display's EDID offers (central/displays/views.py `ModeView`). */
export interface ModeView {
  width: number;
  height: number;
  refresh_millihertz: number;
  preferred: boolean;
}

/** One Display (central/displays/views.py `DisplayView`). */
export interface DisplayView {
  id: string;
  maker: string;
  product: number;
  name: string;
  serial: string | null;
  tied_to_frame: boolean;
  modes: ModeView[];
}

/** GET /v1/operator/frames/{frame_id}/display (central/displays/views.py `FrameDisplayView`). */
export interface FrameDisplayView {
  frame_id: string;
  readiness: Readiness;
  player_id: string | null;
  output_id: string | null;
  display: DisplayView | null;
  position_display: DisplayView | null;
  connected: boolean | null;
  reported_at: number | null;
}

/** How often the Hardware tab re-reads its Display while it is shown. */
const CADENCE_MS = 10000;

type Read = { ok: true; view: FrameDisplayView } | { ok: false; error: string } | null;

/** The mode the display asks for: the one its EDID marks preferred, else its first; null for none. */
export function preferredMode(display: DisplayView | null): ModeView | null {
  return display?.modes.find((mode) => mode.preferred) ?? display?.modes[0] ?? null;
}

const size = (mode: { width: number; height: number }) => `${mode.width} × ${mode.height}`;
const rate = (mode: ModeView) => `${Math.round(mode.refresh_millihertz / 1000)} Hz`;
const named = (display: DisplayView) => display.name || `${display.maker} ${display.product}`;

/** The serial line: the serial, or how a display without a usable one is recognised. */
function serialWords(display: DisplayView): string {
  return display.serial ?? "Recognised by make and model on this Frame";
}

/** The Display's identity and its detected modes, read-only. */
export function DisplayFacts({ display }: { display: DisplayView }) {
  return (
    <>
      <FactRow label="Make" tone="reported">{display.maker}</FactRow>
      <FactRow label="Model" tone="reported">{named(display)}</FactRow>
      <FactRow label="Serial" tone="reported">{serialWords(display)}</FactRow>
      <FactGroup title="Detected modes">
        {display.modes.length === 0 ? <Note>The display listed no modes.</Note> : display.modes.map((mode) => (
          <FactRow key={`${mode.width}x${mode.height}@${mode.refresh_millihertz}`} tone="reported">
            {`${size(mode)} at ${rate(mode)}${mode.preferred ? " (preferred)" : ""}`}
          </FactRow>
        ))}
      </FactGroup>
    </>
  );
}

export interface DisplayChangedCardProps {
  view: FrameDisplayView;
  onConfirmProfile: () => void;
  onRecheckPosition: () => void;
}

/**
 * The display-changed card (design-language §8 row G5; console design §6): another display is on
 * this Frame's HDMI port, so the Frame shows nothing until step 1, the Frame profile confirmed or
 * corrected (offered at the new display's preferred mode), and step 2, its Position checked again.
 * Done on the Position tab clears it.
 */
export function DisplayChangedCard({ view, onConfirmProfile, onRecheckPosition }: DisplayChangedCardProps) {
  const now = view.display;
  const before = view.position_display;
  const mode = preferredMode(now);
  return (
    <Stack label="Display changed">
      <ProblemCard
        scope="setup"
        subject="Display"
        variant="inline"
        verdict={{ severity: "todo", label: "Not ready", receipt: null }}
        what={`A different display is on this Frame's HDMI port${now === null ? "" : `: ${named(now)} (${serialWords(now)})`}. This Frame shows no photos until you confirm its Frame profile and check its Position again.`}
        doing={null}
        details={before === null ? undefined : (
          <Note>{`Its Position was set on ${named(before)} (${serialWords(before)}).`}</Note>
        )}
      />
      <FactGroup title="Step 1: Frame profile">
        <Note>
          {mode === null
            ? "The new display listed no modes. Check the Frame profile below and edit it if it is wrong."
            : `The new display asks for ${size(mode)}. Confirm it as this Frame's profile, or correct it with Edit Frame profile below.`}
        </Note>
        {mode !== null && <Button onClick={onConfirmProfile}>Confirm Frame profile</Button>}
      </FactGroup>
      <FactGroup title="Step 2: Position">
        <Note>Check where the picture sits on the new display, then press Done.</Note>
        <Button onClick={onRecheckPosition}>Re-check Position</Button>
      </FactGroup>
    </Stack>
  );
}

interface SnapshotFrame {
  id: string;
  generation?: number;
  width_mm?: number;
  height_mm?: number;
  profile?: { width_px?: number; height_px?: number; diagonal_inches?: number; video?: boolean };
  readiness?: Readiness;
}

/**
 * The Hardware tab's Display (console design §5, §6): the display the Pi last reported on this
 * Frame's HDMI port, its make, model, serial and detected modes, and the display-changed card when
 * the Frame's Position was set on another one. Confirming the profile saves the new display's
 * preferred mode, turned to the Frame's orientation, keeping the profile's diagonal and video; the
 * second step opens the Position tab.
 */
export function FrameDisplay({ frameId }: { frameId: string }) {
  const { snapshot } = useSnapshot();
  const { navigate } = useRoute();
  const mutate = useMutate();
  const [status, setStatus] = useState<string | null>(null);
  const [problem, setProblem] = useState<{ words: string; code: string | null } | null>(null);
  const [saving, setSaving] = useState(false);
  const frame = ((snapshot as { inventory?: { frames?: SnapshotFrame[] } } | null)?.inventory?.frames ?? [])
    .find((candidate) => candidate.id === frameId);

  const load = useCallback(async () => (await readFrameDisplay(frameId)) as Read, [frameId]);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: null as Read });
  // A Frame whose readiness or generation moved (a bind, a profile, a Position commit) is read again.
  useEffect(() => {
    void refresh();
  }, [refresh, frame?.readiness, frame?.generation]);

  const view = read?.ok ? read.view : null;
  const confirmProfile = async () => {
    const mode = preferredMode(view?.display ?? null);
    if (mode === null || frame === undefined || saving) return;
    const turned = orientationCoherent(frame.width_mm ?? 0, frame.height_mm ?? 0, mode.width, mode.height)
      ? { width_px: mode.width, height_px: mode.height } : { width_px: mode.height, height_px: mode.width };
    const profile = { ...turned, diagonal_inches: frame.profile?.diagonal_inches ?? 0, video: Boolean(frame.profile?.video) };
    setProblem(null);
    setStatus(null);
    const invalid = frameProfileProblem(profile, frame);
    if (invalid !== null) {
      setProblem({ words: invalid, code: null });
      return;
    }
    setSaving(true);
    try {
      const result = await mutate(() => updateFrameProfile(frameId, profile, frame.generation ?? 0));
      if (result.ok) setStatus(`Frame profile set to ${size({ width: turned.width_px, height: turned.height_px })}. Now check its Position again.`);
      else setProblem({ words: "The Frame profile was not saved.", code: result.code });
    } catch {
      setProblem({ words: "Photo Wall's server did not answer; the Frame profile was not saved.", code: null });
    } finally {
      setSaving(false);
    }
  };

  let body;
  if (read === null) {
    body = <Note>Reading the display…</Note>;
  } else if (!read.ok) {
    body = (
      <ProblemCard
        scope="central"
        variant="inline"
        verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Photo Wall's server did not answer with this Frame's display."
        doing="The page tries again by itself."
        details={<Note>{`Photo Wall's answer: ${read.error}`}</Note>}
      />
    );
  } else if (read.view.player_id === null) {
    body = <Note>No Pi feeds this Frame yet, so no display is known.</Note>;
  } else if (read.view.display === null) {
    body = <Note>The Pi has not reported a display on this HDMI port yet.</Note>;
  } else {
    const shown = read.view;
    body = (
      <>
        {shown.connected === false && <Note>Not connected now; this is the display last seen on this HDMI port.</Note>}
        {shown.display !== null && <DisplayFacts display={shown.display} />}
        {shown.reported_at !== null && <Note>{`Reported by the Pi at ${clockTime(shown.reported_at)}.`}</Note>}
        {shown.readiness === "display-changed" && (
          <DisplayChangedCard
            view={shown}
            onConfirmProfile={() => { void confirmProfile(); }}
            onRecheckPosition={() => navigate({ section: "wall", id: frameId, tab: "position" }, { replace: true })}
          />
        )}
      </>
    );
  }
  return (
    <Section title="Display">
      {body}
      {status !== null && <Note live>{status}</Note>}
      {problem !== null && (
        <ProblemCard
          scope="setup"
          variant="inline"
          live
          verdict={{ severity: "todo", label: "Not saved", receipt: null }}
          what={`${problem.words} Edit Frame profile below to set it by hand.`}
          doing={null}
          details={problem.code === null ? undefined : <Note>{`Photo Wall's answer: ${problem.code}`}</Note>}
        />
      )}
    </Section>
  );
}
