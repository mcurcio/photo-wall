import type { Meta, StoryObj } from "@storybook/react-vite";

import { NightOffEditor } from "./night-off-editor";
import { NIGHT_OFF, NOW } from "./power-week.fixtures";

const meta = {
  title: "Domain/NightOffEditor",
  component: NightOffEditor,
  args: {
    view: NIGHT_OFF,
    frames: [{ id: "bedroom", name: "Bedroom" }, { id: "hall", name: "Hall" }],
    placeSet: false,
    houseHref: "#/house",
    now: NOW,
    onSave: async () => ({ ok: true as const }),
  },
} satisfies Meta<typeof NightOffEditor>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The default: 23:00 to 07:00 every night on All Frames; no house location, so no sun times. */
export const Default: Story = {};
/** With the house location: off at sunset plus 30 minutes, on chosen Frames, showing black. */
export const SunAndChosenFrames: Story = {
  args: {
    placeSet: true,
    view: { ...NIGHT_OFF, next_off: "2026-10-14T18:42", night_off: { ...NIGHT_OFF.night_off,
      off: { at: "sunset", offset_minutes: 30 }, frames: { kind: "frames", frame_ids: ["bedroom"] },
      darkness: "black-picture", hold_lasts: "until-resumed" } },
  },
};
/** Settings › House never saved: the zone is Central's own, so Night off switches nothing yet. */
export const WaitsForZone: Story = { args: { view: { ...NIGHT_OFF, waits_for_zone: true } } };
