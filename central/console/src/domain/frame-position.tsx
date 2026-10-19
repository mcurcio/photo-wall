import { useEffect, useRef, useState } from "react";
import type * as React from "react";

import { BoxEditor, type BoxHandle } from "../patterns/box-editor";
import { FactGroup, Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { NudgePad } from "../patterns/nudge-pad";
import { ProblemCard } from "../patterns/problem-card";
import { pixelSize } from "../timeWords.js";
import { Button } from "../ui/button";
import { Field, NumberInput } from "../ui/field";
import { SegmentedControl } from "../ui/segmented-control";
import { Section } from "../ui/section";
import { Inline, Stack } from "../ui/stack";
import type { Readiness } from "./frame-facts";
import { FrameSizeFacts } from "./frame-size-facts";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";
import {
  type Box, boxFromPixels, boxPixels, movedBy, resizedBy, screenOf, WHOLE_SCREEN,
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
  /** Whether the Frame is connected to a Pi's port. */
  bound: boolean;
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
// What the arrows do: move the box, or change its size (left and right its width, up and down its
// height, by the bottom-right corner, so the top-left stays put).
const ARROW_MODES = [{ value: "move", label: "Move" }, { value: "size", label: "Size" }] as const;
const SIZE_WORDS = { up: "Shorter", down: "Taller", left: "Narrower", right: "Wider" } as const;
// The screen drawn while its resolution is not known: STAND_IN_WIDTH pixels wide at the Display's
// own shape when it states its size, else 16:9, the common shape.
const STAND_IN_WIDTH = 1920;
const STAND_IN: readonly [number, number] = [STAND_IN_WIDTH, 1080];

/** The screen drawn while its resolution is not known, at the Display's own shape when known. */
function standIn(displayMm: readonly [number, number] | null): readonly [number, number] {
  if (displayMm === null) return STAND_IN;
  return [STAND_IN_WIDTH, Math.max(1, Math.round((STAND_IN_WIDTH * displayMm[1]) / displayMm[0]))];
}

/** The note under an unknown screen: what the drawing stands in at and what its numbers are. */
function unknownSizeWords(drawn: readonly [number, number], displayShape: boolean): string {
  const shape = displayShape ? "the display's own shape" : "16:9";
  return `Photo Wall does not know this screen's resolution yet, so the drawing stands in at ${shape}: the `
    + `numbers are pixels of a ${pixelSize(drawn[0], drawn[1])} stand-in, and what is kept is their shares of `
    + "the screen, not its pixels.";
}
const CHECK_AGAIN = "If the picture sits right on the Display, press Done to keep this position. After a turn "
  + "the box keeps its shares of the screen, so its shape turns with the screen.";
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
 * the Frame's shape: nothing else asks for it.
 *
 * Every number is in pixels of the screen as it hangs, by the one pixel rule Central and the
 * Player use (src/placement.js `placeBox`, through domain/position-box.ts). A box always lies on
 * its screen: a move stops at the edge, and a typed value that would run past it puts the box flush
 * with it, so nothing is refused. Under the drawing, the Frame's pixels and shape and, when the
 * Display states its size, its size in centimetres (FrameSizeFacts, the Hardware tab's words). With
 * no known screen resolution the drawing stands in at the Display's own shape when it states its
 * size, else at 16:9, and a note says the numbers are shares of the screen.
 *
 * Each change is sent to the Pi as it is made (liveAdjustment.js), and Done keeps it. A Frame
 * whose Position must be checked again (its turn changed, or another display is on its port) is
 * kept by Done with no change. The Frame's lighting has its own tab on the same live session.
 */
export function FramePosition({
  adjustment, size, displayMm, turn, bound, piHref, readiness, whatFits, hardwareHref,
}: FramePositionProps) {
  const known = screenOf(size, turn);
  const stand = standIn(displayMm);
  const screen = known ?? screenOf(stand, turn)!;
  const [width, height] = screen;
  const checkAgain = readiness === "position-needed" || readiness === "display-changed";
  const [step, setStep] = useState<(typeof STEPS)[number]>("10");
  const [arrows, setArrows] = useState<(typeof ARROW_MODES)[number]["value"]>("move");
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
  const sizeBy = (dx: number, dy: number) => resize("bottom-right", dx * Number(step), dy * Number(step));
  /** The box with one of its pixel values typed: its size first held on the screen, then its edge. */
  const typed = (field: "x" | "y" | "width" | "height") => (value: number) =>
    propose(boxFromPixels({ ...pixels, [field]: value }, screen));
  const centred = () => propose(boxFromPixels({
    ...pixels, x: Math.round((width - pixels.width) / 2), y: Math.round((height - pixels.height) / 2),
  }, screen));

  const facts = (
    <FactGroup title="This Frame">
      <FrameSizeFacts box={box} screen={known} displayMm={displayMm} turn={turn} bound={bound} />
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
      <Stack label="Step by step">
        <SegmentedControl label="Arrows" value={arrows} options={[...ARROW_MODES]} onChange={setArrows} />
        <SegmentedControl
          label="Step"
          value={step}
          options={STEPS.map((each) => ({ value: each, label: `${each} px` }))}
          onChange={setStep}
        />
        {arrows === "move"
          ? <NudgePad label="Move the picture" subject="the picture" onNudge={nudge} />
          : <NudgePad label="Size the picture" subject="the picture" words={SIZE_WORDS} onNudge={sizeBy} />}
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
          {known === null ? `In pixels of the ${pixelSize(width, height)} stand-in`
            : `In pixels of the ${pixelSize(width, height)} screen as it hangs`}
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
        {known === null && <Note>{unknownSizeWords(screenOf(stand, turn)!, displayMm !== null)}</Note>}

        <BoxEditor
          label="Picture position"
          screen={{ width, height }}
          picture={pixels}
          topEdge={TOP_EDGE[turn]}
          onMove={moveBy}
          onResize={resize}
          onArrow={nudge}
          aside={controls}
          help={<Note>{HOW}</Note>}
          helpSummary="How to move and size it"
        />
        {whatFits}
      </LiveAdjustment>
    </Section>
  );
}
