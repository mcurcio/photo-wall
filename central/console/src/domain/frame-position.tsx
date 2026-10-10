import { useState } from "react";

import { FactGroup, Note } from "../patterns/fact-row";
import { Message } from "../patterns/message";
import { NudgePad } from "../patterns/nudge-pad";
import { type Point, QuadEditor } from "../patterns/quad-editor";
import { Button } from "../ui/button";
import { NumberField, SelectField } from "../ui/field";
import { Row, Stack } from "../ui/row";
import { Section } from "../ui/section";
import { type Adjustment, LiveAdjustment, adjustable } from "./live-adjustment";

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
const STEPS = [1, 10, 50] as const;
const FULL_SCREEN: Point[] = [[0, 0], [1, 0], [1, 1], [0, 1]];
const EDGE = "The picture cannot go past the edge of the screen: move a corner inward first.";

const round = (value: number) => Math.round(value * 10) / 10;
const inside = (value: number) => value >= 0 && value <= 1;

/**
 * The Frame page's Position tab: where the picture sits on the Display. Drag a corner on the
 * drawing, or choose what to move (the whole picture or one corner) and press the arrows (or the
 * arrow keys on the drawing) in steps of 1, 10 or 50 pixels; type a corner's pixels; rotate; trim
 * each edge. Every change goes to the Display as it is made (liveAdjustment.js), and Done keeps it.
 * A change that would fold the picture or leave the screen is refused where it is made.
 */
export function FramePosition({ adjustment, size, softwareHref }: FramePositionProps) {
  const [selected, setSelected] = useState<number | null>(null);
  const [step, setStep] = useState<number>(10);
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
    const moveX = (dx * step) / size.width;
    const moveY = (dy * step) / size.height;
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
      {!adjustable(adjustment) ? (
        <LiveAdjustment adjustment={adjustment} noun="position" softwareHref={softwareHref} />
      ) : (
        <Stack>
          <Note>
            Drag a corner (or an edge&apos;s square to trim it), or choose what to move and use the
            arrows. Each change shows on the Display as you make it; press Done to keep it.
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
          {problem !== null && <Message kind="alert">{problem}</Message>}
          <Row label="Nudge">
            <SelectField
              label="Move"
              value={selected === null ? "all" : String(selected)}
              options={[{ value: "all", label: "Whole picture" },
                ...CORNERS.map((name, index) => ({ value: String(index), label: `${name} corner` }))]}
              onValueChange={(value) => setSelected(value === "all" ? null : Number(value))}
            />
            <SelectField
              label="Step"
              value={String(step)}
              options={STEPS.map((pixels) => ({ value: String(pixels), label: `${pixels} px` }))}
              onValueChange={(value) => setStep(Number(value))}
            />
            <NudgePad label="Move the picture" subject={subject} onNudge={nudge} />
          </Row>
          <LiveAdjustment adjustment={adjustment} noun="position" softwareHref={softwareHref} />
          <FactGroup title={`Corners, in pixels of the Pi's ${size.width} × ${size.height} output`}>
            <Row>
              {CORNERS.map((name, index) => (
                [0, 1].map((axis) => (
                  <NumberField
                    key={`${name}-${axis}`}
                    label={`${name} ${axis === 0 ? "x" : "y"}`}
                    step={1}
                    value={round(corners[index][axis] * (axis === 0 ? size.width : size.height))}
                    onValueChange={(raw) => setPixel(index, axis as 0 | 1, raw)}
                  />
                ))
              ))}
            </Row>
          </FactGroup>
          <FactGroup title="Trim edges, in percent of the screen">
            <Row>
              {(["Trim left", "Trim top", "Trim right", "Trim bottom"] as const).map((label, edge) => (
                <NumberField
                  key={label}
                  label={label}
                  min={0}
                  max={100}
                  step={0.5}
                  value={round((edge < 2 ? draft.crop[edge] : 1 - draft.crop[edge]) * 100)}
                  onValueChange={(raw) => setTrim(edge as 0 | 1 | 2 | 3, raw)}
                />
              ))}
            </Row>
          </FactGroup>
          <Row>
            <SelectField
              label="Rotation"
              value={String(draft.rotation)}
              options={[0, 90, 180, 270].map((angle) => ({ value: String(angle), label: `${angle}°` }))}
              onValueChange={(value) => apply(adjustment.change({ rotation: Number(value) }))}
            />
            <Button onClick={() => apply(adjustment.change({
              corners: FULL_SCREEN.map(([x, y]) => [x, y]), crop: [0, 0, 1, 1],
            }))}>
              Reset to full screen
            </Button>
          </Row>
        </Stack>
      )}
    </Section>
  );
}
