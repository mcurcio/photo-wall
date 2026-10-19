import type { Meta, StoryObj } from "@storybook/react-vite";

import { BoxEditor } from "./box-editor";

const meta = {
  title: "Patterns/BoxEditor",
  component: BoxEditor,
  args: {
    label: "Picture position",
    screen: { width: 1920, height: 1080 },
    picture: { x: 240, y: 135, width: 1100, height: 810 },
    onMove: () => {},
    onResize: () => {},
    onArrow: () => {},
  },
} satisfies Meta<typeof BoxEditor>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A landscape screen with a box of its own shape on it: four edge bars and four corner squares in the accent. */
export const Default: Story = {};
/** A screen hung with its top to the right: drawn portrait, its own top edge thicker, a tall box on it. */
export const Turned: Story = {
  args: { screen: { width: 1080, height: 1920 }, picture: { x: 90, y: 400, width: 600, height: 1200 }, topEdge: "right" },
};
/** A small box in a corner: its handles crowd, the corners take the pointer, the name is left out. */
export const Small: Story = { args: { picture: { x: 1700, y: 900, width: 160, height: 120 } } };
/** The whole screen: the handles sit on the screen's own edges. */
export const WholeScreen: Story = { args: { picture: { x: 0, y: 0, width: 1920, height: 1080 } } };
/** Disabled: the box is drawn, with no handle to drag. */
export const Disabled: Story = { args: { disabled: true } };
