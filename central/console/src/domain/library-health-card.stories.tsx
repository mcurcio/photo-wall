import type { Meta, StoryObj } from "@storybook/react-vite";

import { LibraryHealthCard } from "./library-health-card";
import { connectionWith } from "./library.fixtures";

const meta = {
  title: "Domain/LibraryHealthCard",
  component: LibraryHealthCard,
  args: { connection: connectionWith({ kind: "key_rejected" }), onFix: () => {} },
} satisfies Meta<typeof LibraryHealthCard>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Immich rejected the key: Paste a new key. */
export const KeyRejected: Story = {};

export const MissingPermissions: Story = {
  args: { connection: connectionWith({ kind: "missing_permissions", missing: ["person.read", "face.read"] }) },
};

export const ImmichTooOld: Story = {
  args: { connection: connectionWith({ kind: "immich_too_old", found: "2.4.1", oldest: "2.5.0" }) },
};

export const Unreachable: Story = { args: { connection: connectionWith({ kind: "unreachable" }) } };

/** The code is under Details. */
export const UnexpectedAnswer: Story = {
  args: { connection: connectionWith({ kind: "unexpected_answer", detail: "statistics_shape" }) },
};

export const OtherUser: Story = {
  args: { connection: connectionWith({ kind: "other_user", expected: "Anna", found: "Ben" }) },
};

/** On Needs attention: the fix is a link to Settings › Photo library. */
export const OnNeedsAttention: Story = { args: { onFix: undefined } };
