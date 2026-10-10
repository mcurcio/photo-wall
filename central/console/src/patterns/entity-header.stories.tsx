import type { Meta, StoryObj } from "@storybook/react-vite";

import { EntityHeader } from "./entity-header";
import { FactRow } from "./fact-row";

const meta = {
  title: "Patterns/EntityHeader",
  component: EntityHeader,
  args: {
    title: "Player …a1b2c3",
    back: { text: "All Pis", href: "#/hardware" },
    links: [{ text: "Software and screens", href: "#/players/device-a" }],
    children: <FactRow label="Standing" tone="set">Bound</FactRow>,
  },
} satisfies Meta<typeof EntityHeader>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Full: Story = {};
export const Bare: Story = { args: { back: undefined, links: [], children: undefined } };
