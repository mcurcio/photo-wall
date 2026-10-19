import { useEffect, useState } from "react";
import type * as React from "react";

import { fillScreen, place } from "../placement.js";
import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { NudgePad } from "../patterns/nudge-pad";
import { ProblemCard } from "../patterns/problem-card";
import { type PixelRectangle, RectangleEditor } from "../patterns/rectangle-editor";
import { pixelSize } from "../timeWords.js";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { Field, NumberInput } from "../ui/field";
import { SegmentedControl } from "../ui/segmented-control";
import { Section } from "../ui/section";
import { Slider } from "../ui/slider";
import { Inline, Stack } from "../ui/stack";
import type { Readiness } from "./frame-profile";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";
import {
  edgesCrossed, keptWords, movedInside, offScreenShort, offScreenWords, pixelValues, type Rectangle, screenOf,
  shareWords, widthWords, wouldLie,
} from "./position-on-screen";
import { HANGS, TOP_EDGE, type Turn } from "./turn-words";

/** The output's mode as built, in pixels, as the Pi reported it. */
export interface PixelSize {
  width: number;
  height: number;
}

export interface FramePositionProps {
  adjustment: Adjustment;
  /** The Output's mode as built, as reported; null while the Pi has reported none (steps then use a common one). */
  size: PixelSize | null;
  /** The Frame's turn: how its screen hangs (inventory `turn`). */
  turn: Turn;
  /** The Frame profile's width_px / height_px; null while the profile is not whole. */
  aspect: number | null;
  piHref: string | null;
  /** The Frame's readiness: a Position to check again is kept by Done with no change. */
  readiness?: Readiness;
  /** What would fit with this Position, judged before Done (follow-ups C1: the Frame config
   * moment); the page composes it. */
  whatFits?: React.ReactNode;
  /** The Frame page's Hardware tab, where the turn is set. */
  hardwareHref: string;
}

/** The section's title: what the tab sets, not the tab's own name again. */
export const POSITION_TITLE = "Where the picture sits on the screen";
const STEPS = ["1", "10", "50"] as const;
const UNKNOWN_SIZE = "The Pi has not reported its display's size, so a step is a pixel of a 1,920 × 1,080 display.";
const CHECK_AGAIN = "If the picture sits right on the Display, press Done to keep this position.";
const NO_PROFILE = "Set this Frame's size in pixels (its Frame profile) on the Hardware tab first: the picture keeps that shape.";
// Central's refusal of a Position off the screen as it measures it: said here, with the edge.
const OFF_SCREEN = "position_off_screen";
const OWN_REFUSALS: ReadonlySet<string> = new Set([OFF_SCREEN]);
const BLOCKED = "Done is offered once the picture fits the screen.";
const PRESENTING = "What the Pi is presenting";

/** The rectangle in whole pixels on `screen`, or null where it does not lie wholly on it (or is no rectangle). */
function placed(rectangle: Rectangle, screen: [number, number], aspect: number): PixelRectangle | null {
  try {
    return place(rectangle, screen, aspect);
  } catch {
    return null;
  }
}

interface PixelFieldProps {
  label: string;
  /** The value now, in pixels. */
  value: number;
  /** Applies a typed value: null when taken, else why not (the Field's error). */
  apply: (pixels: number) => string | null;
}

/**
 * A pixel value typed in a Field: the text stays as typed, and a value the editor refuses (one that
 * would leave the screen) is said next to it, never sent. A value changed elsewhere (a drag, a
 * Restore) replaces what was typed.
 */
function PixelField({ label, value, apply }: PixelFieldProps) {
  const [text, setText] = useState(String(value));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setText((typed) => (Number(typed) === value ? typed : String(value)));
    setError(null);
  }, [value]);
  return (
    <Field label={label} error={error ?? undefined}>
      <NumberInput step={1} value={text}
        onChange={(raw) => {
          setText(raw);
          const pixels = Number(raw);
          if (raw.trim() === "" || !Number.isFinite(pixels)) return;
          setError(apply(pixels));
        }} />
    </Field>
  );
}

/** A refused change: the rectangle refused, and whether the editor's own check or Central refused it. */
interface Refused {
  rectangle: Rectangle;
  /** Typed in a Field: the Field says it, so no card. */
  typed: boolean;
}

/**
 * The Frame page's Position tab: where the picture sits on its screen, as the screen hangs (the
 * Frame's turn, set on the Hardware tab, named in one line and marked on the drawing by its top
 * edge). The picture is one upright rectangle of the Frame profile's shape: drag it to move it, or
 * its corner handle to make it larger or smaller (its centre kept); press the arrows (or the arrow
 * keys on the drawing) in steps of 1, 10 or 50 pixels; set its width (a slider and its pixels side
 * by side); Fill the screen or Centre it. Its centre in pixels is under **Exact values**. Every
 * number is in pixels of the screen as it hangs, as History says them (position-on-screen.ts).
 *
 * Every change is judged by the one pixel rule Central and the Player use (src/placement.js
 * `place`) before it is sent: one that would leave the screen is refused where it is made and
 * never reaches the Pi. The refused rectangle is drawn dashed, crossing the edge it breaks, beside
 * what the Pi is presenting; a card ("Not moved") names the edge and offers **Move it back inside**
 * (the nearest position that fits); a typed value says it in its Field instead. Should Central
 * still refuse one (`position_off_screen`: it measured the screen otherwise), the same card says
 * so, the draft is not sent again and Done waits for a position that fits.
 *
 * Each accepted change is sent to the Pi as it is made (liveAdjustment.js), and Done keeps it. A
 * Frame whose Position must be checked again (its picture changed, its turn changed, or another
 * display is on its port) is kept by Done with no change. The Frame's lighting has its own tab on
 * the same live session.
 */
export function FramePosition({
  adjustment, size, turn, aspect, piHref, readiness, whatFits, hardwareHref,
}: FramePositionProps) {
  const screen = screenOf(size, turn);
  const [width, height] = screen;
  const checkAgain = readiness === "position-needed" || readiness === "display-changed";
  const [step, setStep] = useState<(typeof STEPS)[number]>("10");
  const [refusedHere, setRefusedHere] = useState<Refused | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const fit = adjustment.draft.fit as Rectangle;
  const current: Rectangle = { centre_x: fit.centre_x, centre_y: fit.centre_y, width: fit.width };
  const values = pixelValues(current, screen);

  // Every change is a candidate: placed, it is sent; not placed, it is refused here (null: taken,
  // else the refusal's short words, for a typed value's Field).
  const propose = (patch: Partial<Rectangle>, typed = false): string | null => {
    if (aspect === null) return null;
    const candidate = { ...current, ...patch };
    if (placed(candidate, screen, aspect) === null) {
      setRefusedHere({ rectangle: candidate, typed });
      const lies = wouldLie(candidate, screen, aspect);
      return offScreenShort(lies === null ? [] : edgesCrossed(lies, screen));
    }
    setRefusedHere(null);
    const result = adjustment.change(candidate);
    setProblem(result.valid ? null : result.reason ?? null);
    return null;
  };
  const moveBy = (dx: number, dy: number) =>
    propose({ centre_x: current.centre_x + dx / width, centre_y: current.centre_y + dy / height });
  const nudge = (dx: number, dy: number) => moveBy(dx * Number(step), dy * Number(step));
  const setWidth = (pixels: number, typed = false) =>
    (Number.isFinite(pixels) ? propose({ width: pixels / width }, typed) : null);

  // The refused rectangle, if any: Central's refusal of the draft itself, else the editor's own.
  const centralRefused = adjustment.refused && adjustment.error?.code === OFF_SCREEN;
  const refused: Refused | null = centralRefused ? { rectangle: current, typed: false } : refusedHere;
  const presented: Rectangle | null = centralRefused ? adjustment.sessionFit : current;
  const refusedLies = refused === null || aspect === null ? null : wouldLie(refused.rectangle, screen, aspect);
  const edges = refusedLies === null ? [] : edgesCrossed(refusedLies, screen);
  const moveBack = () => {
    if (aspect === null || refused === null) return;
    if (edges.length > 0) propose(movedInside(refused.rectangle, screen, aspect));
    else if (adjustment.sessionFit !== null) propose(adjustment.sessionFit);
  };
  const refusal = refused === null || refused.typed ? undefined : {
    card: (
      <ProblemCard
        scope="action"
        variant="inline"
        live
        verdict={{ severity: "notice", label: "Not moved", receipt: null }}
        what={keptWords(edges)}
        doing={null}
        action={{ label: "Move it back inside", onAction: moveBack }}
        details={centralRefused ? <Note>{`Photo Wall's answer: ${OFF_SCREEN}`}</Note> : undefined}
      />
    ),
    line: offScreenWords(edges),
    blocksDone: centralRefused ? BLOCKED : undefined,
  };

  const turnLine = (
    <OwnerLinks label={`This screen hangs ${HANGS[turn]}.`}
      links={[{ text: "Change it on the Hardware tab", href: hardwareHref }]} />
  );
  const controls = aspect === null ? null : (
    <Stack>
      {problem !== null && (
        <ProblemCard
          scope="action"
          variant="inline"
          live
          verdict={{ severity: "notice", label: "Not moved", receipt: null }}
          what={problem}
          doing={null}
        />
      )}
      <Stack label="Move">
        <SegmentedControl
          label="Step"
          value={step}
          options={STEPS.map((pixels) => ({ value: pixels, label: `${pixels} px` }))}
          onChange={setStep}
        />
        <NudgePad label="Move the picture" subject="the picture" onNudge={nudge} />
      </Stack>
      <Inline label="Size">
        <Field label="Width" help={`${widthWords(values.width)} · ${shareWords(values.width, width)}`}>
          <Slider min={1} max={width} step={1} unit="px" value={values.width}
            onPreview={setWidth} onCommit={setWidth} />
        </Field>
        <PixelField label="Width in pixels" value={values.width} apply={(pixels) => setWidth(pixels, true)} />
      </Inline>
      <Inline>
        <Button onClick={() => propose(fillScreen(screen, aspect))}>Fill the screen</Button>
        <Button onClick={() => propose({ centre_x: 0.5, centre_y: 0.5 })}>Centre</Button>
      </Inline>
    </Stack>
  );
  // A portrait screen is drawn narrow: on a wide page its controls sit beside it.
  const editor = aspect === null ? null : (
    <RectangleEditor
      label="Picture position"
      screen={{ width, height }}
      picture={presented === null ? null : placed(presented, screen, aspect)}
      pictureLabel={refused === null ? "Picture" : PRESENTING}
      refused={refused === null ? null : refusedLies}
      topEdge={TOP_EDGE[turn]}
      onMove={moveBy}
      onResize={(dWidth) => setWidth(values.width + dWidth)}
      onArrow={nudge}
      aside={controls}
    />
  );
  return (
    <Section title={POSITION_TITLE}>
      <LiveAdjustment adjustment={adjustment} noun="position" piHref={piHref}
        unconfirmed={checkAgain ? CHECK_AGAIN : undefined} ownRefusals={OWN_REFUSALS} refusal={refusal}>
        {turnLine}
        {size === null && <Note>{UNKNOWN_SIZE}</Note>}
        {aspect === null ? <Note>{NO_PROFILE}</Note> : (
          <>
            <Note>
              Drag the picture to move it, or the handle at its bottom right to change its size. Each change is
              sent to the Pi as you make it; press Done to keep it.
            </Note>
            {editor}
            {whatFits}
            <Disclosure summary="Exact values">
              <Note>{`In pixels of the ${pixelSize(width, height)} screen as it hangs`}</Note>
              <Inline>
                <PixelField label="Centre across" value={values.across}
                  apply={(pixels) => propose({ centre_x: pixels / width }, true)} />
                <PixelField label="Centre down" value={values.down}
                  apply={(pixels) => propose({ centre_y: pixels / height }, true)} />
              </Inline>
            </Disclosure>
          </>
        )}
      </LiveAdjustment>
    </Section>
  );
}
