import type { Meta, StoryObj } from "@storybook/react-vite";

import { MediaGrid } from "./media-grid";

function swatch(shade: number): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="rgb(${shade},${shade},${shade})"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

const ITEMS = Array.from({ length: 9 }, (_, index) => ({
  key: `item-${index}`,
  src: swatch(60 + index * 18),
  alt: `Photo dated ${index + 1} Mar 2025`,
  video: index === 4,
}));

const meta = {
  title: "Patterns/MediaGrid",
  component: MediaGrid,
  args: { label: "Photos in the pool", items: ITEMS },
} satisfies Meta<typeof MediaGrid>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

/** More can be read: "Show more". */
export const WithMore: Story = { args: { onMore: () => {} } };

export const ReadingMore: Story = { args: { onMore: () => {}, moreBusy: true } };
