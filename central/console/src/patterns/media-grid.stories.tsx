import type { Meta, StoryObj } from "@storybook/react-vite";

import { MediaGrid } from "./media-grid";

/** A photo of `width` x `height`: a gradient with a circle, so its ratio and any crop show. */
function photo(width: number, height: number, shade: number): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`
    + `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="rgb(${shade},${shade},${shade})"/>`
    + `<stop offset="1" stop-color="rgb(${shade + 60},${shade + 60},${shade + 60})"/></linearGradient></defs>`
    + `<rect width="${width}" height="${height}" fill="url(#g)"/>`
    + `<circle cx="${width / 2}" cy="${height / 2}" r="${Math.min(width, height) / 3}" fill="rgb(${shade + 90},${shade + 90},${shade + 90})"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** The varied library's shapes: 3:2, 4:3, 16:9, 9:16, 1:1, a 4:1 panorama, 3:4, 2:3, 4:3. */
const SHAPES: [number, number][] = [[3, 2], [4, 3], [16, 9], [9, 16], [1, 1], [4, 1], [3, 4], [2, 3], [4, 3]];

const ITEMS = SHAPES.map(([w, h], index) => ({
  key: `item-${index}`,
  src: photo(w * 60, h * 60, 50 + index * 12),
  alt: `Photo dated ${index + 1} Mar 2025, ${w}:${h}`,
  width: w * 1000,
  height: h * 1000,
  video: index === 4,
}));

const meta = {
  title: "Patterns/MediaGrid",
  component: MediaGrid,
  args: { label: "Photos in the pool", items: ITEMS },
} satisfies Meta<typeof MediaGrid>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Justified rows, each photo at its own ratio (tests/browser/test_console_catalog_browser.py
 * checks every thumbnail's drawn ratio against its photo's). */
export const Default: Story = {};

/** More can be read: "Show more". */
export const WithMore: Story = { args: { onMore: () => {} } };

export const ReadingMore: Story = { args: { onMore: () => {}, moreBusy: true } };
