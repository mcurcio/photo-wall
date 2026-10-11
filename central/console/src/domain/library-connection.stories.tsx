import type { Meta, StoryObj } from "@storybook/react-vite";

import { LibraryConnection } from "./library-connection";
import { AT, CONNECTED, connectionWith } from "./library.fixtures";

const meta = {
  title: "Domain/LibraryConnection",
  component: LibraryConnection,
  args: {
    connection: CONNECTED,
    checkNote: null,
    onSave: async () => null,
    onCheck: () => {},
    onRemove: async () => null,
  },
} satisfies Meta<typeof LibraryConnection>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Connected: the key's last four characters, the Immich user and Immich's own counts. */
export const Connected: Story = {};

/** A passing check with both notes: shown, nothing stops. */
export const WithNotes: Story = {
  args: {
    connection: {
      ...CONNECTED,
      health: { state: "ok", checked_at: AT, notes: [{ kind: "untested_version", found: "2.7.0" }, { kind: "no_full_size_copies" }] },
    },
  },
};

/** Saved, not checked yet. */
export const Checking: Story = {
  args: {
    connection: { ...CONNECTED, health: { state: "not_checked" }, immich_user: null, counts: null },
    checkNote: "Check requested. Photos keep playing while Photo Wall asks Immich.",
  },
};

/** Needs a look: the problem card with its fix. */
export const NeedsALook: Story = { args: { connection: connectionWith({ kind: "unreachable" }) } };

/** Waiting since the cut-over: its address and key are pasted once. */
export const WaitingForKey: Story = {
  args: {
    connection: {
      ...CONNECTED, url: null, key_hint: null, immich_user: null, counts: null, health: { state: "not_checked" },
    },
  },
};
