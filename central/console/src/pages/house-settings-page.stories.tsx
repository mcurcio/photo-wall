import type { Meta, StoryObj } from "@storybook/react-vite";

import { HouseSettingsForm, type Locate } from "./house-settings-page";

const meta = {
  title: "Pages/HouseSettings",
  component: HouseSettingsForm,
  args: {
    settings: {
      time: { zone: "Europe/London", place: { latitude: 51.5, longitude: -0.13 } }, units: "cm", clock: "24h",
      updated_at: 1_792_000_000,
    },
    browser: "America/New_York",
    locate: (async () => ({ latitude: 40.7128, longitude: -74.006 })) as Locate,
    onSave: async () => ({ ok: false as const, error: "unreachable" }),
  },
} satisfies Meta<typeof HouseSettingsForm>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The house in London, read from a browser in New York (offered, not applied); this device can give its location. */
export const Default: Story = {};
/** No location yet, and a browser that cannot give one: typed coordinates only. */
export const NoLocation: Story = {
  args: { settings: { time: { zone: "Europe/London", place: null }, units: "in", clock: "12h", updated_at: 1_792_000_000 },
    browser: "Europe/London", locate: null },
};
/** At phone width. */
export const Phone: Story = { parameters: { phone: true } };
