import type { Meta, StoryObj } from "@storybook/react-vite";

import { type LutView, LutsSettings, addWords, removeWords } from "./luts-settings";

const BARYTA: LutView = {
  profile_sha256: "a".repeat(64), recipe_id: "lut33-perceptual-relcol-bpc-1-lcms2.16",
  name: "Canson Baryta Photographique II · PRO-1000", added_at: 1_792_000_000,
  used_by: ["Living room left", "Hall"],
};
const MATTE: LutView = {
  profile_sha256: "b".repeat(64), recipe_id: "lut33-perceptual-relcol-bpc-1-lcms2.16",
  name: "Hahnemühle Photo Rag 308 · PRO-1000", added_at: 1_792_090_000, used_by: [],
};

const meta = {
  title: "Domain/LutsSettings",
  component: LutsSettings,
  args: {
    luts: [BARYTA, MATTE],
    note: null,
    adding: false,
    onAdd: () => undefined,
    onRemove: async () => undefined,
  },
} satisfies Meta<typeof LutsSettings>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Two LUTs: one two Frames' paper uses, one no Frame uses. */
export const Listed: Story = {};
/** Nothing on the list yet. */
export const Empty: Story = { args: { luts: [] } };
/** A profile on its way to Central: the chooser waits. */
export const Adding: Story = { args: { adding: true } };
/** A file that is not a printer paper profile, refused in plain words; the list is unchanged. */
export const AddRefused: Story = {
  args: { note: addWords("Display P3.icc", { ok: false, error: "not_a_printer_profile" }) },
};
/** Remove refused: Frames use the LUT, named. */
export const RemoveRefused: Story = {
  args: { note: removeWords(BARYTA, { ok: false, error: "lut_in_use", displays: ["Living room left", "Hall"] }) },
};
/** At phone width. */
export const Phone: Story = { parameters: { phone: true } };
