import { useState } from "react";

import { Note } from "../patterns/fact-row";
import { NudgePad } from "../patterns/nudge-pad";
import { ProblemCard } from "../patterns/problem-card";
import { type Point, QuadEditor } from "../patterns/quad-editor";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { Field, NumberInput } from "../ui/field";
import { SegmentedControl } from "../ui/segmented-control";
import { Select } from "../ui/select";
import { Section } from "../ui/section";
import { Inline } from "../ui/stack";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";

/** The output's size in pixels: what a pixel step and a corner's pixel value are measured in. */
export interface PixelSize {
  width: number;
  height: number;
}

export interface FramePositionProps {
  adjustment: Adjustment;
  size: PixelSize;
  softwareHref: string | null;
}

const CORNERS = ["Top-left", "Top-right", "Bottom-right", "Bottom-left"] as const;
const STEPS = ["1", "10", "50"] as const;
const ROTATIONS = ["0", "90", "180", "270"] as const;
const FULL_SCREEN: Point[] = [[0, 0], [1, 0], [1, 1], [0, 1]];
const EDGE = "The picture cannot go past the edge of the screen: move a corner inward first.";

const round = (value: number) => Math.round(value * 10) / 10;
const inside = (value: number) => value >= 0 && value <= 1;

/**
 * The Frame page's Position tab: where the picture sits on the Display. Drag a corner on the
 * drawing, or choose what to move (the whole picture or one corner) and press the arrows (or the
 * arrow keys on the drawing) in steps of 1, 10 or 50 pixels; rotate; reset. Each corner's pixels
 * and each edge's trim are under **Exact values**. Every change is sent to the Pi as it is made
 * (liveAdjustment.js), and Done keeps it. A change that would fold the picture or leave the
 * screen is refused where it is made.
 */
export function FramePosition({ adjustment, size, softwareHref }: FramePositionProps) {
  const [selected, setSelected] = useState<number | null>(null);
  const [step, setStep] = useState<(typeof STEPS)[number]>("10");
  const [problem, setProblem] = useState<string | null>(null);
  const { draft } = adjustment;
  const corners = draft.corners as unknown as Point[];

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
  const setTrim = (edge: 0 | 1 | 2 | 3, raw: string) => {
    const percent = Number(raw);
    if (raw === "" || Number.isNaN(percent)) return;
    if (percent < 0 || percent > 100) {
      setProblem("A trim is between 0 and 100 %.");
      return;
    }
    const crop = [...draft.crop];
    crop[edge] = edge < 2 ? percent / 100 : 1 - percent / 100;
    apply(adjustment.change({ crop }));
  };
  const subject = selected === null ? "the whole picture" : `the ${CORNERS[selected].toLowerCase()} corner`;

  return (
    <Section title="Position">
      <LiveAdjustment adjustment={adjustment} noun="position" softwareHref={softwareHref}>
        <Note>
          Drag a corner (or an edge&apos;s square to trim it), or choose what to move and use the
          arrows. Each change is sent to the Pi as you make it; press Done to keep it.
        </Note>
        <QuadEditor
          label="Picture position"
          corners={corners}
          crop={draft.crop as [number, number, number, number]}
          aspect={size.width / size.height}
          selected={selected}
          onSelect={setSelected}
          onCorner={(index, point) => setCorners(corners.map((corner, at) => (at === index ? point : corner)))}
          onCrop={(edge, [x, y]) => {
            const crop = [...draft.crop];
            crop[edge] = edge % 2 === 0 ? x : y;
            apply(adjustment.change({ crop }));
          }}
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
            value={String(draft.rotation) as (typeof ROTATIONS)[number]}
            options={ROTATIONS.map((angle) => ({ value: angle, label: `${angle}°` }))}
            onChange={(value) => apply(adjustment.change({ rotation: Number(value) }))}
          />
          <Button onClick={() => apply(adjustment.change({
            corners: FULL_SCREEN.map(([x, y]) => [x, y]), crop: [0, 0, 1, 1],
          }))}>
            Reset to full screen
          </Button>
        </Inline>
        <Disclosure summary="Exact values">
          <Note>{`Corners, in pixels of the Pi's ${size.width} × ${size.height} output`}</Note>
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
          <Note>Trim edges, in percent of the screen</Note>
          <Inline>
            {(["Trim left", "Trim top", "Trim right", "Trim bottom"] as const).map((label, edge) => (
              <Field key={label} label={label}>
                <NumberInput
                  min={0}
                  max={100}
                  step={0.5}
                  value={round((edge < 2 ? draft.crop[edge] : 1 - draft.crop[edge]) * 100)}
                  onChange={(raw) => setTrim(edge as 0 | 1 | 2 | 3, raw)}
                />
              </Field>
            ))}
          </Inline>
        </Disclosure>
      </LiveAdjustment>
    </Section>
  );
}
