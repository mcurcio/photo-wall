import type { Meta, StoryObj } from "@storybook/react-vite";
import { createColumnHelper, tableFeatures, useTable } from "@tanstack/react-table";

import { DataTable } from "./table";

interface Pi {
  name: string;
  link: string;
  temperature: string;
}

const features = tableFeatures({});
const helper = createColumnHelper<typeof features, Pi>();
const columns = helper.columns([
  helper.accessor("name", { header: "Player" }),
  helper.accessor("link", { header: "Link" }),
  helper.accessor("temperature", { header: "Temperature" }),
]);
const ROWS: Pi[] = [
  { name: "pi-03", link: "linked for 2 h", temperature: "48 °C" },
  { name: "pi-07", link: "not linked for 4 min", temperature: "not read" },
  { name: "pi-11", link: "linked for 6 d", temperature: "61 °C" },
];
const NONE: Pi[] = [];

function Players({ data, caption }: { data: Pi[]; caption?: string }) {
  const table = useTable({ features, columns, data });
  return <DataTable table={table} caption={caption} empty="No Players yet." />;
}

const meta = {
  title: "Primitives/Table",
  component: Players,
  args: { data: ROWS, caption: "Driving a Frame" },
} satisfies Meta<typeof Players>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Rows: Story = {};
export const Empty: Story = { args: { data: NONE } };
