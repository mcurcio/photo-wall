import type { Meta, StoryObj } from "@storybook/react-vite";

import type { PaperProfileView } from "../../generated/contract";
import { addWords, removeWords } from "./model";
import { PaperProfiles } from "./paper-profiles";

const BARYTA: PaperProfileView = {
  profile_sha256: "a".repeat(64), recipe_id: "lut33-perceptual-relcol-bpc-1-lcms2.16",
  name: "Canson Baryta Photographique II · PRO-1000", added_at: 1_792_000_000,
  used_by: ["Living room left", "Hall"],
};
const MATTE: PaperProfileView = {
  profile_sha256: "b".repeat(64), recipe_id: "lut33-perceptual-relcol-bpc-1-lcms2.16",
  name: "Hahnemühle Photo Rag 308 · PRO-1000", added_at: 1_792_090_000, used_by: [],
};

const meta = {
  title: "Domain/PaperProfiles",
  component: PaperProfiles,
  args: {
    profiles: [BARYTA, MATTE],
    note: null,
    adding: false,
    onAdd: () => undefined,
    onRemove: async () => null,
  },
} satisfies Meta<typeof PaperProfiles>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Two paper profiles: one two Frames' paper uses, one no Frame uses. */
export const Listed: Story = {};
/** Nothing on the list yet. */
export const Empty: Story = { args: { profiles: [] } };
/** A profile on its way to Central: the chooser waits. */
export const Adding: Story = { args: { adding: true } };
/** A file that is not a printer paper profile, refused in plain words; the list is unchanged. */
export const AddRefused: Story = {
  args: { note: addWords("Display P3.icc", { ok: false, error: "not_a_printer_profile" }) },
};
/** One no Frame uses, removed. */
export const Removed: Story = { args: { profiles: [BARYTA], note: removeWords(MATTE, { ok: true }) } };
/** At phone width. */
export const Phone: Story = { parameters: { phone: true } };
