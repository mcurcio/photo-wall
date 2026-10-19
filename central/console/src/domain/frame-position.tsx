import { useEffect, useRef, useState } from "react";
import type * as React from "react";

import { boxSizeMm } from "../placement.js";
import { BoxEditor, type BoxHandle } from "../patterns/box-editor";
import { FactGroup, FactRow, Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { NudgePad } from "../patterns/nudge-pad";
import { ProblemCard } from "../patterns/problem-card";
import { pixelSize } from "../timeWords.js";
import { Button } from "../ui/button";
import { Field, NumberInput } from "../ui/field";
import { SegmentedControl } from "../ui/segmented-control";
import { Section } from "../ui/section";
import { Inline, Stack } from "../ui/stack";
import type { Readiness } from "./frame-profile";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";
import {
  type Box, boxFromPixels, boxPixels, movedBy, resizedBy, screenOf, shapeWords, sizeWords, WHOLE_SCREEN,
} from "./position-box";
import { HANGS, TOP_EDGE, type Turn } from "./turn-words";

/** A screen's size in pixels (Position history says an entry's box in them). */
export interface PixelSize {
  width: number;
  height: number;
}

export interface FramePositionProps {
  adjustment: Adjustment;
  /** The Frame's screen, the mode as built (Central's `FrameInventory.screen_px`); null when not known. */
  size: readonly [number, number] | null;
  /** The Display's own picture area in millimetres as built (`FrameInventory.display_mm`); null
   * when no Display is seen there or it states none. */
  displayMm: readonly [number, number] | null;
  /** The Frame's turn: how its screen hangs (inventory `turn`). */
  turn: Turn;
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
// The screen drawn while its size is not known: 16:9, the common shape, in its pixels.
const STAND_IN: readonly [number, number] = [1920, 1080];
const UNKNOWN_SIZE = "Photo Wall does not know this screen's size yet, so the drawing stands in at 16:9: the numbers "
  + "are pixels of a 1,920 × 1,080 stand-in, and what is kept is their shares of the screen, not its pixels.";
const CHECK_AGAIN = "If the picture sits right on the Display, press Done to keep this position.";
const HOW = "Drag the picture to move it, or any edge or corner to change its width and height. Each change is "
  + "sent to the Pi as you make it; press Done to keep it.";

interface PixelFieldProps {
  label: string;
  /** The value now, in pixels. */
  value: number;
  /** Takes a typed whole number of pixels (the box stays on its screen, so none is refused). */
  apply: (pixels: number) => void;
}

/**
 * A pixel value typed in a Field: the text stays as typed while it is the value taken, and a value
 * changed elsewhere (a drag, a nudge, a Restore, a value moved back onto the screen) replaces it.
 */
function PixelField({ label, value, apply }: PixelFieldProps) {
  const [text, setText] = useState(String(value));
  useEffect(() => {
    setText((typed) => (Number(typed) === value ? typed : String(value)));
  }, [value]);
  return (
    <Field label={label}>
      <NumberInput step={1} value={text}
        onChange={(raw) => {
          setText(raw);
          const pixels = Number(raw);
          if (raw.trim() !== "" && Number.isFinite(pixels)) apply(Math.round(pixels));
        }} />
    </Field>
  );
}

/**
 * The Frame page's Position tab (frame box run, .claude/runs/frame-box/page.md fb-7; owner,
 * 2026-10-19: "I need to be able to set width and height independently, along with the position of
 * that box on the display"): the Frame's picture is one box on its screen as it hangs (the Frame's
 * turn, set on the Hardware tab, named in one line and marked on the drawing by the display's top
 * edge). Drag the box to move it, or any edge or corner to change its width and height apart
 * (BoxEditor); press the arrows (or the arrow keys on the drawing) in steps of 1, 10 or 50 pixels;
 * type its Left, Top, Width and Height in whole pixels; Fill the screen or Centre it. The box is
 * the Frame's shape: no Frame profile is asked for.
 *
 * Every number is in pixels of the screen as it hangs, by the one pixel rule Central and the
 * Player use (src/placement.js `placeBox`, through domain/position-box.ts). A box always lies on
 * its screen: a move stops at the edge, and a typed value that would run past it puts the box flush
 * with it, so nothing is refused. Under the drawing, the Frame's pixels and shape and, when the
 * Display states its size, its size in centimetres. With no known screen size the drawing is a
 * 16:9 stand-in and a note says the numbers are shares of the screen.
 *
 * Each change is sent to the Pi as it is made (liveAdjustment.js), and Done keeps it. A Frame
 * whose Position must be checked again (its turn changed, or another display is on its port) is
 * kept by Done with no change. The Frame's lighting has its own tab on the same live session.
 */
export function FramePosition({
  adjustment, size, displayMm, turn, piHref, readiness, whatFits, hardwareHref,
}: FramePositionProps) {
  const known = screenOf(size, turn);
  const screen = known ?? screenOf(STAND_IN, turn)!;
  const [width, height] = screen;
  const checkAgain = readiness === "position-needed" || readiness === "display-changed";
  const [step, setStep] = useState<(typeof STEPS)[number]>("10");
  const [problem, setProblem] = useState<string | null>(null);
  const box: Box = adjustment.draft.fit;
  const pixels = boxPixels(box, screen);
  // The box each change starts from: the draft as rendered, or the last change taken since (a drag
  // reports its steps faster than the page draws them, and no step may be lost).
  const latest = useRef(box);
  latest.current = box;

  const propose = (next: Box) => {
    const result = adjustment.change(next);
    if (result.valid) latest.current = next;
    setProblem(result.valid ? null : result.reason ?? null);
  };
  const moveBy = (dx: number, dy: number) => propose(movedBy(latest.current, dx, dy, screen));
  const nudge = (dx: number, dy: number) => moveBy(dx * Number(step), dy * Number(step));
  const resize = (handle: BoxHandle, dx: number, dy: number) =>
    propose(resizedBy(latest.current, handle, dx, dy, screen));
  /** The box with one of its pixel values typed: its size first held on the screen, then its edge. */
  const typed = (field: "x" | "y" | "width" | "height") => (value: number) =>
    propose(boxFromPixels({ ...pixels, [field]: value }, screen));
  const centred = () => propose(boxFromPixels({
    ...pixels, x: Math.round((width - pixels.width) / 2), y: Math.round((height - pixels.height) / 2),
  }, screen));

  const physical = sizeWords(boxSizeMm(box, displayMm === null ? null : [displayMm[0], displayMm[1]], turn));
  const facts = (
    <FactGroup title="This Frame">
      {known !== null && <FactRow label="Shape" tone="derived">{shapeWords(pixels)}</FactRow>}
      {physical !== null && <FactRow label="Size" tone="derived">{physical}</FactRow>}
      {known === null && physical === null && (
        <FactRow tone="unknown">Its pixels and shape are known once Photo Wall knows the screen&apos;s size.</FactRow>
      )}
    </FactGroup>
  );
  const controls = (
    <Stack>
      {facts}
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
          options={STEPS.map((each) => ({ value: each, label: `${each} px` }))}
          onChange={setStep}
        />
        <NudgePad label="Move the picture" subject="the picture" onNudge={nudge} />
      </Stack>
      <Stack label="Size and place">
        <Inline>
          <PixelField label="Left" value={pixels.x} apply={typed("x")} />
          <PixelField label="Top" value={pixels.y} apply={typed("y")} />
        </Inline>
        <Inline>
          <PixelField label="Width" value={pixels.width} apply={typed("width")} />
          <PixelField label="Height" value={pixels.height} apply={typed("height")} />
        </Inline>
        <Note>
          {known === null ? "In pixels of the 16:9 stand-in" : `In pixels of the ${pixelSize(width, height)} screen as it hangs`}
        </Note>
      </Stack>
      <Inline>
        <Button onClick={() => propose(WHOLE_SCREEN)}>Fill the screen</Button>
        <Button onClick={centred}>Centre</Button>
      </Inline>
    </Stack>
  );
  return (
    <Section title={POSITION_TITLE}>
      <LiveAdjustment adjustment={adjustment} noun="position" piHref={piHref}
        unconfirmed={checkAgain ? CHECK_AGAIN : undefined}>
        <OwnerLinks label={`This screen hangs ${HANGS[turn]}.`}
          links={[{ text: "Change it on the Hardware tab", href: hardwareHref }]} />
        {known === null && <Note>{UNKNOWN_SIZE}</Note>}
        <Note>{HOW}</Note>
        <BoxEditor
          label="Picture position"
          screen={{ width, height }}
          picture={pixels}
          topEdge={TOP_EDGE[turn]}
          onMove={moveBy}
          onResize={resize}
          onArrow={nudge}
          aside={controls}
        />
        {whatFits}
      </LiveAdjustment>
    </Section>
  );
}
