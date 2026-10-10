import type { Meta, StoryObj } from "@storybook/react-vite";

import { playersByDevice } from "../players.js";
import { BOOT_FACTS, SNAPSHOT } from "./hardware.fixtures";
import { PiHeader } from "./pi-header";

const [BOUND] = playersByDevice(SNAPSHOT, BOOT_FACTS as never);

const meta = {
  title: "Domain/PiHeader",
  component: PiHeader,
  args: { row: BOUND, on: "hardware" },
} satisfies Meta<typeof PiHeader>;

export default meta;
type Story = StoryObj<typeof meta>;

export const OnHardware: Story = {};
export const OnSoftware: Story = { args: { on: "software" } };
export const WithSerial: Story = { args: { row: { ...BOUND, serial: "10000000a1b2c3", name: "Player …a1b2c3" } } };
