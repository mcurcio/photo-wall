import { useState } from "react";
import type * as React from "react";

import { fillScreen, hanging, place } from "../placement.js";
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
import { Inline } from "../ui/stack";
import type { Readiness } from "./frame-profile";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";
import { TRIAL_REFUSAL_WORDS } from "./trial-refusals";
import { type Turn, TURN_WORDS } from "./turn-words";

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

/** A rectangle's three values: its centre as fractions of the hung screen, its width as a fraction of its width. */
interface Rectangle {
  centre_x: number;
  centre_y: number;
  width: number;
}

const STEPS = ["1", "10", "50"] as const;
// A Pi that reports no output size: steps are still pixels of a common output.
const FALLBACK_SIZE: [number, number] = [1920, 1080];
const UNKNOWN_SIZE = "The Pi has not reported its display's size, so a step is a pixel of a 1,920 × 1,080 display.";
const CHECK_AGAIN = "If the picture sits right on the Display, press Done to keep this position.";
const NO_PROFILE = "Set this Frame's size in pixels (its Frame profile) on the Hardware tab first: the picture keeps that shape.";
const OFF_SCREEN = TRIAL_REFUSAL_WORDS.position_off_screen;

const round = (value: number) => Math.round(value * 10) / 10;

/** The rectangle in whole pixels on `screen`, or null where it does not lie wholly on it (or is no rectangle). */
function placed(rectangle: Rectangle, screen: [number, number], aspect: number): PixelRectangle | null {
  try {
    return place(rectangle, screen, aspect);
  } catch {
    return null;
  }
}

/**
 * The Frame page's Position tab: where the picture sits on its screen, as the screen hangs (the
 * Frame's turn, set on the Hardware tab). The picture is one upright rectangle of the Frame
 * profile's shape: drag it to move it, or its corner handle to make it larger or smaller (its
 * centre kept); press the arrows (or the arrow keys on the drawing) in steps of 1, 10 or 50
 * pixels; set its width; Fill the screen or Centre it. Its centre and width in pixels are under
 * **Exact values**. Every change is judged by the one pixel rule Central and the Player use
 * (src/placement.js `place`) before it is sent: one that would leave the screen is refused where
 * it is made and never reaches the Pi. Each accepted change is sent to the Pi as it is made
 * (liveAdjustment.js), and Done keeps it. A Frame whose Position must be checked again (its
 * picture changed, its turn changed, or another display is on its port) is kept by Done with no
 * change. The Frame's lighting has its own tab on the same live session.
 */
export function FramePosition({
  adjustment, size, turn, aspect, piHref, readiness, whatFits, hardwareHref,
}: FramePositionProps) {
  const screen = hanging(size === null ? FALLBACK_SIZE : [size.width, size.height], turn) as [number, number];
  const [width, height] = screen;
  const checkAgain = readiness === "position-needed" || readiness === "display-changed";
  const [step, setStep] = useState<(typeof STEPS)[number]>("10");
  const [problem, setProblem] = useState<string | null>(null);
  const fit = adjustment.draft.fit as Rectangle;
  const current: Rectangle = { centre_x: fit.centre_x, centre_y: fit.centre_y, width: fit.width };

  // Every change is a candidate: placed, it is sent; not placed, it is refused here.
  const propose = (patch: Partial<Rectangle>) => {
    if (aspect === null) return;
    const candidate = { ...current, ...patch };
    if (placed(candidate, screen, aspect) === null) {
      setProblem(OFF_SCREEN);
      return;
    }
    const result = adjustment.change(candidate);
    setProblem(result.valid ? null : result.reason ?? null);
  };
  const moveBy = (dx: number, dy: number) =>
    propose({ centre_x: current.centre_x + dx / width, centre_y: current.centre_y + dy / height });
  const nudge = (dx: number, dy: number) => moveBy(dx * Number(step), dy * Number(step));
  const setWidth = (pixels: number) => {
    if (Number.isFinite(pixels)) propose({ width: pixels / width });
  };
  const typed = (raw: string, apply: (pixels: number) => void) => {
    const pixels = Number(raw);
    if (raw !== "" && Number.isFinite(pixels)) apply(pixels);
  };

  const turnLine = (
    <OwnerLinks label={`The screen hangs: ${TURN_WORDS[turn]}.`}
      links={[{ text: "Change it on the Hardware tab", href: hardwareHref }]} />
  );
  return (
    <Section title="Position">
      <LiveAdjustment adjustment={adjustment} noun="position" piHref={piHref}
        unconfirmed={checkAgain ? CHECK_AGAIN : undefined}>
        {turnLine}
        {size === null && <Note>{UNKNOWN_SIZE}</Note>}
        {aspect === null ? <Note>{NO_PROFILE}</Note> : (
          <>
            <Note>
              Drag the picture to move it, or the handle at its bottom right to change its size. Each change is
              sent to the Pi as you make it; press Done to keep it.
            </Note>
            <RectangleEditor
              label="Picture position"
              screen={{ width, height }}
              picture={placed(current, screen, aspect)}
              onMove={moveBy}
              onResize={(dWidth) => setWidth(Math.round(current.width * width) + dWidth)}
              onArrow={nudge}
            />
            {problem !== null && (
              <ProblemCard
                scope="setup"
                variant="inline"
                live
                verdict={{ severity: "todo", label: "Not applied", receipt: null }}
                what={problem}
                doing={null}
              />
            )}
            <Inline label="Nudge">
              <SegmentedControl
                label="Step"
                value={step}
                options={STEPS.map((pixels) => ({ value: pixels, label: `${pixels} px` }))}
                onChange={setStep}
              />
              <NudgePad label="Move the picture" subject="the picture" onNudge={nudge} />
            </Inline>
            <Inline label="Size">
              <Field label="Width">
                <Slider min={1} max={width} step={1} unit="px" value={Math.round(current.width * width)}
                  onPreview={setWidth} onCommit={setWidth} />
              </Field>
              <Field label="Width in pixels">
                <NumberInput min={1} max={width} step={1} value={Math.round(current.width * width)}
                  onChange={(raw) => typed(raw, setWidth)} />
              </Field>
            </Inline>
            <Inline>
              <Button onClick={() => propose(fillScreen(screen, aspect))}>Fill the screen</Button>
              <Button onClick={() => propose({ centre_x: 0.5, centre_y: 0.5 })}>Centre</Button>
            </Inline>
            {whatFits}
            <Disclosure summary="Exact values">
              <Note>{`In pixels of the ${pixelSize(width, height)} screen as it hangs`}</Note>
              <Inline>
                <Field label="Centre across">
                  <NumberInput step={1} value={round(current.centre_x * width)}
                    onChange={(raw) => typed(raw, (pixels) => propose({ centre_x: pixels / width }))} />
                </Field>
                <Field label="Centre down">
                  <NumberInput step={1} value={round(current.centre_y * height)}
                    onChange={(raw) => typed(raw, (pixels) => propose({ centre_y: pixels / height }))} />
                </Field>
                <Field label="Picture width">
                  <NumberInput step={1} value={round(current.width * width)}
                    onChange={(raw) => typed(raw, setWidth)} />
                </Field>
              </Inline>
            </Disclosure>
          </>
        )}
      </LiveAdjustment>
    </Section>
  );
}
