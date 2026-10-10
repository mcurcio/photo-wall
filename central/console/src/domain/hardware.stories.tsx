import type { Meta, StoryObj } from "@storybook/react-vite";

import { HardwareList } from "./hardware-list";
import { BOOT_FACTS, HOSTS, HOSTS_FAILED, SNAPSHOT } from "./hardware.fixtures";
import { HostHealthLink } from "./host-health-link";
import { HostHealthPanel } from "./host-health";

const meta = {
  title: "Domain/HardwareList",
  component: HardwareList,
  args: { snapshot: SNAPSHOT, bootFacts: BOOT_FACTS, hosts: HOSTS, focus: null },
} satisfies Meta<typeof HardwareList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Driving a Frame worst first, Not driving a Frame, Retired. */
export const Groups: Story = {};
/** Focused on one Pi (`?pi=`). */
export const Focused: Story = { args: { focus: "device-a" } };
/** Node control not on: the host columns are not shown. */
export const NoHostRead: Story = { args: { hosts: null } };
/** The fleet host read failed with no earlier result. */
export const ReadFailed: Story = { args: { hosts: HOSTS_FAILED } };
export const Empty: Story = { args: { snapshot: { inventory: { read_at: 1000, players: [] } } } };

/** One Pi's Health section. */
export const Health: Story = { render: () => <HostHealthPanel hosts={HOSTS} deviceId="device-b" /> };
/** A Frame's host link, as the Frame page's Overview shows it. */
export const HostLink: Story = {
  render: () => <HostHealthLink snapshot={SNAPSHOT} frameId="lobby-right" hosts={HOSTS} />,
};
