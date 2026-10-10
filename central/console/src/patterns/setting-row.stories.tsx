import type { Meta, StoryObj } from "@storybook/react-vite";

import { RangeField } from "../ui/field";
import { Switch } from "../ui/switch";
import { SettingRow } from "./setting-row";

const fade = (value: number) => (
  <RangeField
    label="Fade between photos"
    value={value}
    min={0}
    max={5}
    step={0.5}
    valueText={value === 0 ? "Off" : `${value} s`}
    onValueChange={() => {}}
  />
);

const meta = {
  title: "Patterns/SettingRow",
  component: SettingRow,
  args: {
    kind: "content",
    label: "Fade between photos",
    help: "Each photo fades out and the next fades in, through black.",
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
/** An equipment setting says where it acts. */
export const EquipmentOnDisplay: Story = {
  args: {
    kind: "equipment",
    belongsTo: "display",
    actsOn: "display-hardware",
    label: "Switch to this input on power-on",
    help: undefined,
    defaultLabel: "Default: on",
    control: <Switch label="Switch to this input on power-on" checked onCheckedChange={() => {}} />,
  },
};
export const EquipmentPictureAdjustment: Story = {
  args: { kind: "equipment", belongsTo: "frame", actsOn: "picture-adjustment" },
};
export const Saving: Story = { args: { kind: "house", state: "saving" } };
export const Failed: Story = { args: { kind: "house", state: "error", error: "Central did not answer." } };
