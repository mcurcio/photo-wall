import { Note } from "../patterns/fact-row";
import { RangeField } from "../ui/field";
import { Stack } from "../ui/row";
import { Section } from "../ui/section";
import { type Adjustment, LiveAdjustment, adjustable } from "./live-adjustment";

export interface FramePictureProps {
  adjustment: Adjustment;
  softwareHref: string | null;
}

/**
 * The Frame page's Picture tab: Brightness, the picture adjustment Photo Wall makes in the
 * picture it sends (the calibration's gain, 0 to 2, shown as 0 to 200 %). It is not the
 * Display's own brightness and never claims to be (requirements: software gain is never
 * called measured panel brightness). Changes go to the Display live; Done keeps them.
 */
export function FramePicture({ adjustment, softwareHref }: FramePictureProps) {
  const percent = Math.round(adjustment.draft.gain * 100);
  return (
    <Section title="Picture">
      <Stack>
        {adjustable(adjustment) && (
          <>
            <RangeField
              label="Brightness — Photo Wall picture adjustment"
              hint="Photo Wall brightens or dims the picture it sends; the Display's own settings do not change. 100 % leaves it as it is."
              min={0}
              max={2}
              step={0.05}
              value={adjustment.draft.gain}
              valueText={`${percent} %`}
              onValueChange={(gain) => adjustment.change({ gain: Math.round(gain * 100) / 100 })}
            />
            <Note>Each change shows on the Display as you make it; press Done to keep it.</Note>
          </>
        )}
        <LiveAdjustment adjustment={adjustment} noun="picture" softwareHref={softwareHref} />
      </Stack>
    </Section>
  );
}
