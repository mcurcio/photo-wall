import type { Meta, StoryObj } from "@storybook/react-vite";

import type { Severity } from "../design/tokens";
import { type EntityColumn, EntityList } from "./entity-list";
import { FocusFilter } from "./focus-filter";

interface Row {
  key: string;
  severity: Severity | null;
  name: string;
  state: string;
}

const columns: EntityColumn<Row>[] = [
  { id: "name", header: "Pi", cell: (row) => row.name, rowHeader: true },
  { id: "state", header: "State", cell: (row) => row.state },
];

const driving: Row[] = [
  { key: "a", severity: "ok", name: "pi-01", state: "Reporting" },
  { key: "b", severity: "alarm", name: "pi-02", state: "Throttled now" },
  { key: "c", severity: "unknown", name: "pi-03", state: "Not read" },
  { key: "d", severity: "notice", name: "pi-04", state: "Warm" },
];
const spares: Row[] = [{ key: "e", severity: null, name: "pi-05", state: "Unbound" }];

const meta = {
  title: "Patterns/EntityList",
  component: EntityList<Row>,
  args: {
    label: "Pis",
    columns,
    empty: "No Pis yet.",
    groups: [
      { title: "Driving a Frame", rows: driving, worstFirst: true },
      { title: "Not driving a Frame", rows: spares },
    ],
  },
} satisfies Meta<typeof EntityList<Row>>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Groups, the first worst first. */
export const Grouped: Story = {};
/** No row in any group: the empty state. */
export const Empty: Story = { args: { groups: [{ title: "Driving a Frame", rows: [] }] } };
/** Narrowed to one row, with its focus chip. */
export const Focused: Story = {
  args: {
    groups: [{ title: "Driving a Frame", rows: driving.filter((row) => row.key === "b"), worstFirst: true }],
    notes: <FocusFilter focused="pi-02" noun="Pi" clearHref="#/hardware" />,
  },
};
