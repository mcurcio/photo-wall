import type { Meta, StoryObj } from "@storybook/react-vite";

import { SegmentedControl } from "../ui/segmented-control";
import { Slider } from "../ui/slider";
import { Switch } from "../ui/switch";
import { SettingRow } from "./setting-row";

const fade = (value: number, disabled = false) => (
  <Slider value={value} min={0} max={5} step={0.5} unit="s" onCommit={() => {}} disabled={disabled} />
);

const meta = {
  title: "Patterns/SettingRow",
  component: SettingRow,
  args: {
    kind: "content",
    label: "Fade between photos",
    help: "Each photo fades out and the next fades in, through black or what plays beneath.",
    control: fade(1.5),
    defaultLabel: "Default: 1.5 s",
    state: "idle",
  },
} satisfies Meta<typeof SettingRow>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A content setting at its default: no Reset. */
export const AtDefault: Story = {};
/** Changed from its default: Reset names what it resets to. */
export const ChangedFromDefault: Story = { args: { control: fade(3), onReset: () => {} } };
/** A choice of a few, as How it ends. */
export const Choice: Story = {
  args: {
    label: "How it ends",
    help: "When the Scene finishes: stop at once, cover its Frames with black, or the last photo "
      + "returns and fades out to what plays beneath.",
    defaultLabel: "Default: Stops",
    control: (
      <SegmentedControl
        label="How it ends"
        value="black"
        options={[{ value: "none", label: "Stops" }, { value: "black", label: "Black" },
          { value: "fade", label: "Fades out" }]}
        onChange={() => {}}
      />
    ),
    onReset: () => {},
  },
};
/** An equipment setting says where it acts. */
export const EquipmentOnDisplay: Story = {
  args: {
    kind: "equipment",
    belongsTo: "display",
    actsOn: "display-hardware",
    label: "Switch to this input on power-on",
    help: undefined,
    defaultLabel: "Default: On",
    control: <Switch checked onChange={() => {}} />,
  },
};
export const EquipmentPictureAdjustment: Story = {
  args: { kind: "equipment", belongsTo: "frame", actsOn: "picture-adjustment" },
};
/** The control cannot be used now; the help says why. */
export const Disabled: Story = {
  args: { control: fade(1.5, true), help: "Seconds per cycle is too short for a fade." },
};
/** A value that cannot be kept, said under the control. */
export const Invalid: Story = {
  args: { control: fade(5), error: "Fade between photos must be no longer than Seconds per cycle." },
};
export const Saving: Story = { args: { kind: "house", state: "saving" } };
export const Error: Story = { args: { kind: "house", state: "error", error: "Central did not answer." } };
