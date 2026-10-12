import type { Meta, StoryObj } from "@storybook/react-vite";

import { FrameMat, type LightDraft, NEW_LIGHT } from "./frame-mat";

// The owner's window Frame (2026-10-12): cool white light from the side, warm white from the top.
const WINDOW: LightDraft = { from_degrees: 90, steep_degrees: 30, softness_degrees: 25, strength_tenths: 15, colour_kelvin: 7500 };
const LAMP: LightDraft = { from_degrees: 0, steep_degrees: 60, softness_degrees: 5, strength_tenths: 10, colour_kelvin: 2700 };

const meta = {
  title: "Domain/FrameMat",
  component: FrameMat,
  args: { mat: { depth_mm: 0, lights: [] }, onChange: () => {}, disabled: false },
} satisfies Meta<typeof FrameMat>;

export default meta;
type Story = StoryObj<typeof meta>;

/** No lights: no shadow, the picture as it was. */
export const NoLights: Story = {};
// A story taller than the catalogue's 800 x 600 view is screenshot whole (tag "full-page",
// tests/browser/test_console_catalog_browser.py), so its baseline holds the state it exists to show.
const WHOLE = ["full-page"];

/** One light just added: from the wall's up, steep 45°, softness 10°, strength 1.0, 6500 K, at depth 2.0 mm. */
export const OneLight: Story = { tags: WHOLE, args: { mat: { depth_mm: 2, lights: [{ ...NEW_LIGHT }] } } };
/** By a window: a cool light from the right and a warm one from the top. */
export const Window: Story = { tags: WHOLE, args: { mat: { depth_mm: 2.5, lights: [WINDOW, LAMP] } } };
/** Four lights: Add is disabled, and the note says why. */
export const Full: Story = {
  tags: WHOLE,
  args: { mat: { depth_mm: 1.5, lights: [WINDOW, LAMP, { ...NEW_LIGHT, from_degrees: 180 }, { ...NEW_LIGHT, from_degrees: 270 }] } },
};
/** Lights but no depth: no shadow yet. */
export const NoDepth: Story = { args: { mat: { depth_mm: 0, lights: [LAMP] } } };
/** While Done or Revert runs, or the Pi stopped taking changes: nothing can change. */
export const Disabled: Story = { tags: WHOLE, args: { mat: { depth_mm: 2, lights: [LAMP] }, disabled: true } };
