import type { Meta, StoryObj } from "@storybook/react-vite";
import * as React from "react";

import { Switch } from "./switch";

const meta = {
  title: "Primitives/Switch",
  component: Switch,
  args: {
    label: "Keep the last photo up",
    checked: true,
    onCheckedChange: () => {},
  },
} satisfies Meta<typeof Switch>;

export default meta;
type Story = StoryObj<typeof meta>;

function Controlled(args: React.ComponentProps<typeof Switch>) {
  const [checked, setChecked] = React.useState(args.checked);
  return <Switch {...args} checked={checked} onCheckedChange={setChecked} />;
}

export const On: Story = { render: (args) => <Controlled {...args} /> };
export const Off: Story = { args: { checked: false }, render: (args) => <Controlled {...args} /> };
export const WithHint: Story = {
  args: { hint: "When no new photo can play, the Frame keeps the last one up instead of going dark." },
  render: (args) => <Controlled {...args} />,
};
export const Disabled: Story = { args: { disabled: true } };
