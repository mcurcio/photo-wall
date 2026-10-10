import { Field } from "../ui/field";
import { Section } from "../ui/section";
import { Slider } from "../ui/slider";
import { type Adjustment, LiveAdjustment } from "./live-adjustment";

export interface FramePictureProps {
  adjustment: Adjustment;
  softwareHref: string | null;
}

/**
 * The Frame page's Picture tab: Brightness, the picture adjustment Photo Wall makes in the
 * picture it sends (the calibration's gain, 0 to 2, shown as 0 to 200 %). It is not the
 * Display's own brightness and never claims to be (requirements: software gain is never
 * called measured panel brightness). Each change is sent to the Pi as it is made; Done keeps it.
 */
export function FramePicture({ adjustment, softwareHref }: FramePictureProps) {
  const percent = Math.round(adjustment.draft.gain * 100);
  const set = (value: number) => adjustment.change({ gain: Math.round(value) / 100 });
  return (
    <Section title="Picture">
      <LiveAdjustment adjustment={adjustment} noun="picture" softwareHref={softwareHref}>
        <Field
          label="Brightness — Photo Wall picture adjustment"
          help="Photo Wall brightens or dims the picture it sends; the Display's own settings do not change. 100 % leaves it as it is. Each change is sent to the Pi as you make it; press Done to keep it."
        >
          <Slider min={0} max={200} step={5} unit="%" value={percent} onPreview={set} onCommit={set} />
        </Field>
      </LiveAdjustment>
    </Section>
  );
}
