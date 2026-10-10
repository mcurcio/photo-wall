import type { Meta, StoryObj } from "@storybook/react-vite";

import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow, TableRowHeader } from "./table";

interface Pi {
  name: string;
  link: string;
  temperature: string;
}

const ROWS: Pi[] = [
  { name: "pi-03", link: "linked for 2 h", temperature: "48 °C" },
  { name: "pi-07", link: "not linked for 4 min", temperature: "not read" },
  { name: "pi-11", link: "linked for 6 d", temperature: "61 °C" },
];
const NONE: Pi[] = [];

function Players({ data, caption }: { data: Pi[]; caption?: string }) {
  return (
    <Table>
      {caption !== undefined && <TableCaption>{caption}</TableCaption>}
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Player</TableHead>
          <TableHead scope="col">Link</TableHead>
          <TableHead scope="col">Temperature</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.length === 0 ? (
          <TableRow>
            <TableCell colSpan={3} className="text-muted">No Players yet.</TableCell>
          </TableRow>
        ) : (
          data.map((pi) => (
            <TableRow key={pi.name}>
              <TableRowHeader>{pi.name}</TableRowHeader>
              <TableCell>{pi.link}</TableCell>
              <TableCell>{pi.temperature}</TableCell>
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  );
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
