import { useCallback, useEffect } from "react";

import { readFrameDisplay } from "../displayApi.js";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { usePolledRead } from "../polledRead.js";
import { clockTime, pixelSize } from "../timeWords.js";
import { Button } from "../ui/button";
import { Section } from "../ui/section";
import { Stack } from "../ui/stack";
import { useRoute } from "../useRoute.js";
import { useSnapshot } from "../useSnapshot.js";
import type { Readiness } from "./frame-profile";
import { sizeWords } from "./position-box";

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
  /** Its picture area in millimetres as built, as its EDID states it; null when it states none. */
  size_mm: [number, number] | null;
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

const size = (mode: { width: number; height: number }) => pixelSize(mode.width, mode.height);
const rate = (mode: ModeView) => `${Math.round(mode.refresh_millihertz / 1000)} Hz`;
const named = (display: DisplayView) => display.name || `${display.maker} ${display.product}`;

/** The serial line: the serial, or how a display without a usable one is recognised. */
function serialWords(display: DisplayView): string {
  return display.serial ?? "Recognised by make and model on this Frame";
}

/** The Display's identity, its size and its detected modes, read-only. */
export function DisplayFacts({ display }: { display: DisplayView }) {
  return (
    <>
      <FactRow label="Make" tone="reported">{display.maker}</FactRow>
      <FactRow label="Model" tone="reported">{named(display)}</FactRow>
      <FactRow label="Serial" tone="reported">{serialWords(display)}</FactRow>
      <FactRow label="Size" tone="reported">{sizeWords(display.size_mm) ?? "Does not state its size"}</FactRow>
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
  onRecheckPosition: () => void;
}

/**
 * The display-changed card (design-language §8 row G5; console design §6): another display is on
 * this Frame's HDMI port, so the Frame shows nothing until its Position is checked on the new
 * display (the Frame's shape and size follow from its Position and the display, frame box run
 * fb-11). Done on the Position tab clears it.
 */
export function DisplayChangedCard({ view, onRecheckPosition }: DisplayChangedCardProps) {
  const now = view.display;
  const before = view.position_display;
  return (
    <Stack label="Display changed">
      <ProblemCard
        scope="setup"
        subject="Display"
        variant="inline"
        verdict={{ severity: "todo", label: "Not ready", receipt: null }}
        what={`A different display is on this Frame's HDMI port${now === null ? "" : `: ${named(now)} (${serialWords(now)})`}. This Frame shows no photos until you check its Position on the new display.`}
        doing={null}
        details={before === null ? undefined : (
          <Note>{`Its Position was set on ${named(before)} (${serialWords(before)}).`}</Note>
        )}
      />
      <FactGroup title="Position">
        <Note>Check where the picture sits on the new display, then press Done.</Note>
        <Button onClick={onRecheckPosition}>Re-check Position</Button>
      </FactGroup>
    </Stack>
  );
}

interface SnapshotFrame {
  id: string;
  generation?: number;
  readiness?: Readiness;
}

/**
 * The Hardware tab's Display (console design §5, §6): the display the Pi last reported on this
 * Frame's HDMI port, its make, model, serial, size and detected modes, and the display-changed card
 * when the Frame's Position was set on another one, which opens the Position tab.
 */
export function FrameDisplay({ frameId }: { frameId: string }) {
  const { snapshot } = useSnapshot();
  const { navigate } = useRoute();
  const frame = ((snapshot as { inventory?: { frames?: SnapshotFrame[] } } | null)?.inventory?.frames ?? [])
    .find((candidate) => candidate.id === frameId);

  const load = useCallback(async () => (await readFrameDisplay(frameId)) as Read, [frameId]);
  const { value: read, refresh } = usePolledRead(load, { cadenceMs: CADENCE_MS, initial: null as Read });
  // A Frame whose readiness or generation moved (a bind, a turn, a Position commit) is read again.
  useEffect(() => {
    void refresh();
  }, [refresh, frame?.readiness, frame?.generation]);

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
            onRecheckPosition={() => navigate({ section: "wall", id: frameId, tab: "position" }, { replace: true })}
          />
        )}
      </>
    );
  }
  return <Section title="Display">{body}</Section>;
}
