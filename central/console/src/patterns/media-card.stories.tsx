import type { Meta, StoryObj } from "@storybook/react-vite";

import { MediaCard, MediaCardAdd, MediaCardList, MediaCardListItem } from "./media-card";

function photo(width: number, height: number, shade: number): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">`
    + `<rect width="${width}" height="${height}" fill="rgb(${shade},${shade},${shade})"/>`
    + `<circle cx="${width / 2}" cy="${height / 2}" r="${Math.min(width, height) / 3}" fill="rgb(${shade + 70},${shade + 70},${shade + 70})"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

const MOSAIC = [[160, 120], [90, 160], [160, 90], [120, 120]].map(([w, h], index) => ({
  key: `p${index}`,
  src: photo(w, h, 60 + index * 20),
  alt: `Photo dated ${index + 1} Mar 2025`,
}));

const meta = {
  title: "Patterns/MediaCard",
  component: MediaCard,
  args: {
    title: "garden",
    href: "#/sources/garden/edit/query",
    mosaic: MOSAIC,
    line: "2,301 photos · 14 videos",
    detail: "Anna and Ben · last 3 years · no screenshots",
    verdict: { severity: "ok", label: "Checked Sun 03:39", receipt: null },
    menu: { label: "More actions for garden", items: [{ label: "Check now", onAction: () => {} }, { label: "Delete", onAction: () => {} }] },
  },
} satisfies Meta<typeof MediaCard>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A saved Source on the Sources list. */
export const Default: Story = {};

/** A Source with no photos yet. */
export const Empty: Story = {
  args: { mosaic: [], line: "0 photos · 0 videos", verdict: { severity: "todo", label: "Not checked yet", receipt: null } },
};

/** A choice in the Scene editor's Photos step, chosen. */
export const Chosen: Story = {
  args: {
    href: undefined,
    menu: undefined,
    choice: { type: "checkbox", checked: true, value: "garden:1", onChange: () => {}, label: "Choose garden" },
  },
};

/** The cards as a list, with "New Photo source" last. */
export const List: Story = {
  render: (args) => (
    <MediaCardList label="Photo sources">
      <MediaCardListItem><MediaCard {...args} /></MediaCardListItem>
      <MediaCardListItem><MediaCard {...args} title="family" mosaic={MOSAIC.slice(0, 1)} /></MediaCardListItem>
      <MediaCardListItem><MediaCardAdd label="New Photo source" onAdd={() => {}} /></MediaCardListItem>
    </MediaCardList>
  ),
};
