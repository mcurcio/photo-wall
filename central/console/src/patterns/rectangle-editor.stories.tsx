import type { Meta, StoryObj } from "@storybook/react-vite";

import { RectangleEditor } from "./rectangle-editor";

const meta = {
  title: "Patterns/RectangleEditor",
  component: RectangleEditor,
  args: {
    label: "Picture position",
    screen: { width: 1920, height: 1080 },
    picture: { x: 240, y: 135, width: 1440, height: 810 },
    onMove: () => {},
    onResize: () => {},
    onArrow: () => {},
  },
} satisfies Meta<typeof RectangleEditor>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A landscape screen with the picture inside it, its corner handle in the accent. */
export const Default: Story = {};
/** A screen hung turned a quarter: drawn portrait, the picture upright on it. */
export const Turned: Story = {
  args: { screen: { width: 1080, height: 1920 }, picture: { x: 90, y: 400, width: 900, height: 1200 } },
};
/** Disabled: the picture is drawn, with no handle to drag. */
export const Disabled: Story = { args: { disabled: true } };
/** The picture does not lie on the screen: the screen alone. */
export const NoPicture: Story = { args: { picture: null } };
