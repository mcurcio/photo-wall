import type { Meta, StoryObj } from "@storybook/react-vite";

import { PhotoLine } from "./photo-line";

/** A same-document picture at a photo's ratio, so a story reaches no server. */
const swatch = (shade: number, width: number, height: number) => `data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="${width / 50}" height="${height / 50}">`
  + `<rect width="100%" height="100%" fill="rgb(${60 + shade * 20},${60 + shade * 20},${60 + shade * 20})"/></svg>`)}`;

const meta = {
  title: "Patterns/PhotoLine",
  component: PhotoLine,
  args: { src: swatch(3, 1600, 1067), children: "A photo" },
} satisfies Meta<typeof PhotoLine>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A landscape photo, whole in its box, with its words. */
export const Default: Story = {};
/** A portrait clip: the box keeps its size, the picture is not cropped. */
export const Portrait: Story = { args: { src: swatch(5, 900, 1600), children: "A clip" } };
