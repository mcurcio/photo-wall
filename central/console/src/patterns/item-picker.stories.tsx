import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";

import { ItemPicker } from "./item-picker";

interface Album { ref: string; name: string }

const ALBUMS: Album[] = [
  { ref: "a", name: "Garden" },
  { ref: "b", name: "Holidays 2024" },
  { ref: "c", name: "Birthdays" },
];

function Picking({ start, max, note }: { start: string[]; max?: number; note?: string }) {
  const [value, setValue] = useState(start);
  return (
    <ItemPicker<Album>
      label="Albums"
      help="Photos in every chosen album."
      search={async (q) => ({ items: ALBUMS.filter((a) => a.name.toLowerCase().includes(q.toLowerCase())), note: null })}
      byId={async (keys) => ALBUMS.filter((a) => keys.includes(a.ref))}
      value={value}
      onChange={(keys) => setValue(keys)}
      keyOf={(album) => album.ref}
      render={(album) => album.name}
      max={max}
      chosenNote={note}
    />
  );
}

const meta = {
  title: "Patterns/ItemPicker",
  component: Picking,
  args: { start: [] },
} satisfies Meta<typeof Picking>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {};

/** Two chosen: the rule about several shows under them. */
export const TwoChosen: Story = {
  args: { start: ["a", "b"], note: "All of these must be in the photo." },
};

/** A single choice, made: the field waits until it is removed. */
export const SingleChosen: Story = { args: { start: ["c"], max: 1 } };

/** A chosen item the library no longer has. */
export const Deleted: Story = { args: { start: ["a", "gone"] } };
