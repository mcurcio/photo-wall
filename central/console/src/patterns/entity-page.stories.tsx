import type { Meta, StoryObj } from "@storybook/react-vite";

import { Section } from "../ui/section";
import { EntityHeader } from "./entity-header";
import { EmptyState, EntityPage } from "./entity-page";
import { FactRow } from "./fact-row";

const meta = {
  title: "Patterns/EntityPage",
  component: EntityPage,
  args: {
    header: <EntityHeader title="Player …a1b2c3" back={{ text: "All Pis", href: "#/hardware" }} />,
    children: (
      <>
        <Section title="Health">
          <FactRow label="Temperature" tone="reported">Host Management last reported 3 s ago · 52 °C</FactRow>
        </Section>
        <Section title="Danger zone">
          <EmptyState>Retire is offered only for a Pi that drives no Frame.</EmptyState>
        </Section>
      </>
    ),
  },
} satisfies Meta<typeof EntityPage>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Sections: Story = {};
export const Unknown: Story = {
  args: { header: undefined, children: <EmptyState>An unknown Pi is not known to Central.</EmptyState> },
};
