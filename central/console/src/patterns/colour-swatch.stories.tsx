import type { Meta, StoryObj } from "@storybook/react-vite";

import { ColourSwatch } from "./colour-swatch";

const meta = {
  title: "Patterns/ColourSwatch",
  component: ColourSwatch,
  args: { colour: "rgb(255 204 153)", children: "Following its bulb's colour" },
} satisfies Meta<typeof ColourSwatch>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A bulb in a colour mode: its rgb as a swatch before the words. */
export const Colour: Story = {};
/** A kelvin, said in full by its words: no swatch. */
export const Words: Story = { args: { colour: null, children: "Following its bulb 3,400 K" } };
