import type { useFrameAdjustment } from "../liveAdjustment.js";
import { Message } from "../patterns/message";
import { Button } from "../ui/button";
import { Row, Stack } from "../ui/row";

/** The Frame page's live adjustment, as liveAdjustment.js serves it. */
export type Adjustment = ReturnType<typeof useFrameAdjustment>;

/** Whether the Frame's Pi can show changes live, so the controls are worth showing. */
export function adjustable(adjustment: Adjustment): boolean {
  return !["checking", "unsupported", "unavailable", "unbound"].includes(adjustment.phase);
}

export interface LiveAdjustmentProps {
  adjustment: Adjustment;
  /** What is adjusted, for the words: "position", "picture". */
  noun: string;
  /** The Frame's Pi's Software page, for a Pi that cannot show changes live. */
  softwareHref: string | null;
}

function words(adjustment: Adjustment, noun: string): string {
  switch (adjustment.phase) {
    case "checking":
      return "Checking whether this Frame's Pi can show changes live…";
    case "unsupported":
      return `This Frame's Pi runs software that cannot show changes live, so its ${noun} cannot be changed here. Update the Pi's software, then come back.`;
    case "unavailable":
      return "Photo Wall could not check this Frame's Pi.";
    case "unbound":
      return "Choose which Pi and HDMI output feed this Frame first (Hardware tab).";
    case "idle":
    case "connecting":
      return "Connecting to the Display…";
    case "waiting":
      return "Someone else is adjusting this Frame. Photo Wall connects as soon as they finish.";
    case "sending":
      return "Sending your change to the Display…";
    case "shown":
      if (adjustment.dirty) return "The Pi is showing your change. Press Done to keep it, or Revert to undo it.";
      return adjustment.saved > 0 ? `Saved. The Pi is showing the saved ${noun}.` : `The Pi is showing the saved ${noun}.`;
  }
}

/**
 * The live line and the two answers of a live adjustment (Position, Picture): what the Display
 * is being sent, **Done** once the Pi has shown the latest change, and **Revert**.
 */
export function LiveAdjustment({ adjustment, noun, softwareHref }: LiveAdjustmentProps) {
  const live = adjustable(adjustment);
  return (
    <Stack label="Show on the Display">
      <Message kind="status">
        {words(adjustment, noun)}
        {adjustment.phase === "unsupported" && softwareHref !== null ? (
          <>
            {" "}
            <a href={softwareHref}>Open the Pi&apos;s software page</a>
          </>
        ) : null}
      </Message>
      {adjustment.error !== null && <Message kind="alert">{adjustment.error}</Message>}
      <Row>
        {live && (
          <>
            <Button variant="primary" disabled={!adjustment.canDone} onClick={() => adjustment.done()}>
              Done
            </Button>
            <Button disabled={!adjustment.dirty || adjustment.busy} onClick={() => adjustment.revert()}>
              Revert
            </Button>
          </>
        )}
        {(adjustment.error !== null || adjustment.phase === "unavailable") && (
          <Button variant="ghost" onClick={() => adjustment.retry()}>Try again</Button>
        )}
      </Row>
    </Stack>
  );
}
