import { useState } from "react";
import type * as React from "react";

import { Note } from "../patterns/fact-row";
import { NudgePad } from "../patterns/nudge-pad";
import { ProblemCard } from "../patterns/problem-card";
import { type Point, QuadEditor } from "../patterns/quad-editor";
import { pixelSize } from "../timeWords.js";
import { QUARTER_TURNS } from "../useDraft.js";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { Field, NumberInput } from "../ui/field";
import { SegmentedControl } from "../ui/segmented-control";
import { Select } from "../ui/select";
import { Section } from "../ui/section";
import { Slider } from "../ui/slider";
import { Inline } from "../ui/stack";
import type { Readiness } from "./frame-profile";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";

/** The output's size in pixels: what a pixel step and a corner's pixel value are measured in. */
export interface PixelSize {
  width: number;
  height: number;
}

export interface FramePositionProps {
  adjustment: Adjustment;
  /** The output's size in pixels; null while the Pi has reported none (steps then use a common one). */
  size: PixelSize | null;
  piHref: string | null;
  /** The Frame's readiness: a Position to check again is kept by Done with no change. */
  readiness?: Readiness;
  /** What would fit with this Position, judged before Done (follow-ups C1: the Frame config
   * moment); the page composes it. */
  whatFits?: React.ReactNode;
}

type Mode = "cover" | "corners";

const CORNERS = ["Top-left", "Top-right", "Bottom-right", "Bottom-left"] as const;
const STEPS = ["1", "10", "50"] as const;
const FULL_SCREEN: Point[] = [[0, 0], [1, 0], [1, 1], [0, 1]];
const EDGE = "The picture cannot go past the edge of the screen: move a corner inward first.";
const ANGLE_MAX = 359.9;
const FITS: readonly { value: Mode; label: string }[] = [
  { value: "cover", label: "Cover" },
  { value: "corners", label: "Corners" },
];
const FIT_HELP: Record<Mode, string> = {
  cover: "The picture keeps its shape, turned to any angle, and covers all four corners; nothing shows outside them.",
  corners: "Stretch the picture exactly onto the corners. Its shape follows the corners; it turns only in quarter turns.",
};

// A Pi that reports no output size: steps are still pixels of a common output.
const FALLBACK_SIZE: PixelSize = { width: 1920, height: 1080 };
const UNKNOWN_SIZE = "The Pi has not reported its display's size, so a step is a pixel of a 1,920 × 1,080 display.";
const CHECK_AGAIN = "If the picture sits right on the Display, press Done to keep this position.";

const round = (value: number) => Math.round(value * 10) / 10;
const inside = (value: number) => value >= 0 && value <= 1;

/**
 * The Frame page's Position tab: where the picture sits on the Display. **Fit**: Cover (the
 * default, owner 2026-10-10 q1) keeps the picture's shape, turned to any angle, and covers the
 * four corners; Corners, an explicit choice, stretches it exactly onto them. Drag a corner on the
 * drawing, or choose what to move (the whole picture or one corner) and press the arrows (or the
 * arrow keys on the drawing) in steps of 1, 10 or 50 pixels; turn it a quarter at a time, or in
 * Cover to any angle; reset. Each corner's pixels are under **Exact values**. Every change is sent
 * to the Pi as it is made (liveAdjustment.js), and Done keeps it. A Frame whose Position must be
 * checked again (its picture changed, or another display is on its port) is kept by Done with no
 * change. A change that would fold the picture or leave the screen is refused where it is made.
 */
export function FramePosition({ adjustment, size: reported, piHref, readiness, whatFits }: FramePositionProps) {
  const size = reported ?? FALLBACK_SIZE;
  const checkAgain = readiness === "position-needed" || readiness === "display-changed";
  const [selected, setSelected] = useState<number | null>(null);
  const [step, setStep] = useState<(typeof STEPS)[number]>("10");
  const [problem, setProblem] = useState<string | null>(null);
  const { fit } = adjustment.draft;
  const corners = fit.corners as unknown as Point[];
  const cover = fit.mode === "cover";

  const apply = (result: { valid: boolean; reason?: string }) => setProblem(result.valid ? null : result.reason ?? null);
  const setCorners = (next: Point[]) => {
    if (!next.every(([x, y]) => inside(x) && inside(y))) {
      setProblem(EDGE);
      return;
    }
    apply(adjustment.change({ corners: next.map(([x, y]) => [x, y]) }));
  };
  const nudge = (dx: number, dy: number) => {
    const moveX = (dx * Number(step)) / size.width;
    const moveY = (dy * Number(step)) / size.height;
    setCorners(corners.map(([x, y], index) =>
      selected === null || selected === index ? [x + moveX, y + moveY] as Point : [x, y] as Point));
  };
  const setPixel = (index: number, axis: 0 | 1, raw: string) => {
    const pixels = Number(raw);
    if (raw === "" || Number.isNaN(pixels)) return;
    const value = pixels / (axis === 0 ? size.width : size.height);
    setCorners(corners.map((corner, at) =>
      at === index ? (axis === 0 ? [value, corner[1]] : [corner[0], value]) as Point : corner));
  };
  const setAngle = (degrees: number) => {
    if (Number.isNaN(degrees) || degrees < 0 || degrees > ANGLE_MAX) {
      setProblem(`An angle is from 0 to ${ANGLE_MAX} degrees.`);
      return;
    }
    apply(adjustment.change({ rotation: round(degrees) }));
  };
  const subject = selected === null ? "the whole picture" : `the ${CORNERS[selected].toLowerCase()} corner`;

  return (
    <Section title="Position">
      <LiveAdjustment adjustment={adjustment} noun="position" piHref={piHref}
        unconfirmed={checkAgain ? CHECK_AGAIN : undefined}>
        {reported === null && <Note>{UNKNOWN_SIZE}</Note>}
        <Field label="Fit" help={FIT_HELP[fit.mode]}>
          <SegmentedControl label="Fit" value={fit.mode} options={FITS}
            onChange={(mode) => apply(adjustment.change({ mode }))} />
        </Field>
        <Note>
          Drag a corner, or choose what to move and use the arrows. Each change is sent to the Pi
          as you make it; press Done to keep it.
        </Note>
        <QuadEditor
          label="Picture position"
          corners={corners}
          aspect={size.width / size.height}
          selected={selected}
          onSelect={setSelected}
          onCorner={(index, point) => setCorners(corners.map((corner, at) => (at === index ? point : corner)))}
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
          <Field label="Move">
            <Select
              value={selected === null ? "all" : String(selected)}
              options={[{ value: "all", label: "Whole picture" },
                ...CORNERS.map((name, index) => ({ value: String(index), label: `${name} corner` }))]}
              onChange={(value) => setSelected(value === "all" ? null : Number(value))}
            />
          </Field>
          <SegmentedControl
            label="Step"
            value={step}
            options={STEPS.map((pixels) => ({ value: pixels, label: `${pixels} px` }))}
            onChange={setStep}
          />
          <NudgePad label="Move the picture" subject={subject} onNudge={nudge} />
        </Inline>
        <Inline>
          <SegmentedControl
            label="Rotation"
            value={String(fit.rotation)}
            options={QUARTER_TURNS.map((angle) => ({ value: String(angle), label: `${angle}°` }))}
            onChange={(value) => apply(adjustment.change({ rotation: Number(value) }))}
          />
          <Button onClick={() => apply(adjustment.change({ corners: FULL_SCREEN.map(([x, y]) => [x, y]), rotation: 0 }))}>
            Reset to full screen
          </Button>
        </Inline>
        {cover && (
          <Inline>
            <Field label="Angle">
              <Slider min={0} max={ANGLE_MAX} step={0.1} unit="°" value={fit.rotation}
                onPreview={setAngle} onCommit={setAngle} />
            </Field>
            <Field label="Angle in degrees">
              <NumberInput min={0} max={ANGLE_MAX} step={0.1} value={fit.rotation}
                onChange={(raw) => { if (raw !== "") setAngle(Number(raw)); }} />
            </Field>
          </Inline>
        )}
        {whatFits}
        <Disclosure summary="Exact values">
          <Note>{`Corners, in pixels of the ${pixelSize(size.width, size.height)} display`}</Note>
          <Inline>
            {CORNERS.map((name, index) => (
              [0, 1].map((axis) => (
                <Field key={`${name}-${axis}`} label={`${name} ${axis === 0 ? "x" : "y"}`}>
                  <NumberInput
                    step={1}
                    value={round(corners[index][axis] * (axis === 0 ? size.width : size.height))}
                    onChange={(raw) => setPixel(index, axis as 0 | 1, raw)}
                  />
                </Field>
              ))
            ))}
          </Inline>
        </Disclosure>
      </LiveAdjustment>
    </Section>
  );
}
