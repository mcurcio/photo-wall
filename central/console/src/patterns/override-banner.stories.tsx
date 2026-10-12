import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "../ui/button";
import { Inline } from "../ui/stack";
import { Note } from "./fact-row";
import { OverrideBanner } from "./override-banner";

const meta = {
  title: "Patterns/OverrideBanner",
  component: OverrideBanner,
  args: {
    label: "Holds",
    title: "Held off until 23:00",
    children: (
      <Inline>
        <Note>Held off · console · until 23:00 · Bedroom, Hall</Note>
        <Button>Resume schedule</Button>
      </Inline>
    ),
  },
} satisfies Meta<typeof OverrideBanner>;

export default meta;
type Story = StoryObj<typeof meta>;

/** All off in force until Night off, with its way back. */
export const Default: Story = {};
/** At phone width: the line wraps and the button stays whole. */
export const Phone: Story = { parameters: { phone: true } };
