import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";

import { type SourceQuery, SourceQueryEditor, defaultQuery } from "./source-query-editor";
import { ANNA_AND_BEN, LOOKUPS } from "./source.fixtures";

const ONE = [{ connection_ref: "library-0123456789ab", name: "Family" }];

function Editing({ start, connections = ONE }: { start: SourceQuery; connections?: typeof ONE }) {
  const [value, setValue] = useState(start);
  return <SourceQueryEditor connections={connections} value={value} onChange={setValue} lookups={LOOKUPS} />;
}

const meta = {
  title: "Domain/SourceQueryEditor",
  component: Editing,
  args: { start: defaultQuery("library-0123456789ab") },
} satisfies Meta<typeof Editing>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A new Source: every photo, no screenshots; the connection is hidden when there is one. */
export const New: Story = {};

/** Two albums and two people: all of them must be in the photo. */
export const AllOfThese: Story = { args: { start: ANNA_AND_BEN } };

/** Several connections: the photo library is chosen first. */
export const SeveralConnections: Story = {
  args: { connections: [...ONE, { connection_ref: "library-ba9876543210", name: "Work" }] },
};

export const BetweenDates: Story = {
  args: { start: { ...defaultQuery("library-0123456789ab"), taken: { kind: "between", start: null, end: null } } },
};
